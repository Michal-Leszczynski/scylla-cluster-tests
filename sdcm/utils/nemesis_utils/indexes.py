# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright (c) 2023 ScyllaDB

import logging
import random
import time
from contextlib import nullcontext

from cassandra.query import SimpleStatement  # pylint: disable=no-name-in-module

from sdcm.cluster import BaseNode
from sdcm.sct_events import Severity
from sdcm.sct_events.database import DatabaseLogEvent
from sdcm.sct_events.filters import EventsFilter
from sdcm.sct_events.system import InfoEvent

LOGGER = logging.getLogger(__name__)


def is_cf_a_view(node: BaseNode, ks, cf) -> bool:
    """
    Check if a CF is a materialized-view or not (a normal table)
    """
    with node.parent_cluster.cql_connection_patient(node) as session:
        try:
            result = session.execute(f"SELECT view_name FROM system_schema.views"
                                     f" WHERE keyspace_name = '{ks}'"
                                     f" AND view_name = '{cf}'")
            return result and bool(len(result.one()))
        except Exception as exc:  # pylint: disable=broad-except
            LOGGER.debug('Got no result from system_schema.views for %s.%s table. Error: %s', ks, cf, exc)
            return False


def get_column_names(session, ks, cf, is_primary_key: bool = False, filter_out_collections: bool = False) -> list:
    filter_kind = " kind in ('static', 'regular')" if not is_primary_key else "kind in ('partition_key', 'clustering')"
    res = session.execute(f"SELECT column_name, type FROM system_schema.columns"
                          f" WHERE keyspace_name = '{ks}'"
                          f" AND table_name = '{cf}'"
                          f" AND {filter_kind}"
                          f" ALLOW FILTERING")
    res_list = list(res)
    column_names = [row.column_name for row in res_list]
    if filter_out_collections:
        collection_types = ('list', 'set', 'map')
        column_names = [row.column_name for row in res_list if not str(row.type).startswith(collection_types)]
    return column_names


def get_random_column_name(session, ks, cf, filter_out_collections: bool = False) -> str | None:
    if column_names := get_column_names(session=session, ks=ks, cf=cf, filter_out_collections=filter_out_collections):
        return random.choice(column_names)
    return None


def create_index(session, ks, cf, column) -> str:
    InfoEvent(message=f"Starting creating index: {ks}.{cf}({column})").publish()
    index_name = f"{cf}_{column}_nemesis".lower()
    session.execute(f'CREATE INDEX {index_name} ON {ks}.{cf}("{column}")')
    return index_name


def wait_for_index_to_be_built(node: BaseNode, ks, index_name, timeout=300) -> None:
    wait_for_view_to_be_built(node=node, ks=ks, view_name=f'{index_name}_index', timeout=timeout)


def wait_for_view_to_be_built(node: BaseNode, ks, view_name, timeout=300) -> None:
    LOGGER.info('waiting for view/index %s to be built', view_name)
    start_time = time.time()
    with (EventsFilter(  # ignore apply view errors when multiple nemesis are running scylladb/scylla-cluster-tests#6469
            event_class=DatabaseLogEvent.DATABASE_ERROR,
            regex=".*view - Error applying view update to.*",
            extra_time_to_expiration=180,
    ) if node.parent_cluster.nemesis_count > 1 else nullcontext()):
        while time.time() - start_time < timeout:
            result = node.run_nodetool(f"viewbuildstatus {ks}.{view_name}",
                                       ignore_status=True, verbose=False, publish_event=False)
            if f"{ks}.{view_name}_index has finished building" in result.stdout:
                InfoEvent(message=f"Index {ks}.{view_name} was built").publish()
            if f"{ks}.{view_name} has finished building" in result.stdout:
                InfoEvent(message=f"View/index {ks}.{view_name} was built").publish()
                return
            time.sleep(30)
    raise TimeoutError(f"Timeout error while creating view/index {view_name}. "
                       f"stdout\n: {result.stdout}\n"
                       f"stderr\n: {result.stderr}")


def verify_query_by_index_works(session, ks, cf, column) -> None:
    # get some value from table to use it in query by index
    result = session.execute(SimpleStatement(f'SELECT "{column}" FROM {ks}.{cf} limit 1', fetch_size=1))
    value = list(result)[0][0]
    if value is None:
        # scylla does not support 'is null' in where clause: https://github.com/scylladb/scylladb/issues/8517
        InfoEvent(message=f"No value for column {column} in {ks}.{cf}, skipping querying created index.",
                  severity=Severity.NORMAL).publish()
        return
    match type(value).__name__:
        case "str" | "datetime":
            value = str(value).replace("'", "''")
            value = f"'{value}'"
        case "bytes":
            value = "0x" + value.hex()
    try:
        query = SimpleStatement(f'SELECT * FROM {ks}.{cf} WHERE "{column}" = {value} LIMIT 100', fetch_size=100)
        LOGGER.debug("Verifying query by index works: %s", query)
        result = session.execute(query)
    except Exception as exc:  # pylint: disable=broad-except
        InfoEvent(message=f"Index {ks}.{cf}({column}) does not work in query: {query}. Reason: {exc}",
                  severity=Severity.ERROR).publish()
    if len(list(result)) == 0:
        InfoEvent(message=f"Index {ks}.{cf}({column}) does not work. No rows returned for query {query}",
                  severity=Severity.ERROR).publish()


def drop_index(session, ks, index_name) -> None:
    InfoEvent(message=f"Starting dropping index: {ks}.{index_name}").publish()
    with EventsFilter(
            event_class=DatabaseLogEvent.DATABASE_ERROR,
            regex=".*view - Error applying view update to.*",
            extra_time_to_expiration=180,  # errors can happen only within few minutes after index drop scylladb/#12977
    ):
        session.execute(f'DROP INDEX {ks}.{index_name}')


def drop_materialized_view(session, ks, view_name) -> None:
    LOGGER.info('start dropping MV: %s.%s', ks, view_name)
    with EventsFilter(
            event_class=DatabaseLogEvent.DATABASE_ERROR,
            regex=".*Error applying view update.*",
            extra_time_to_expiration=180):
        session.execute(f'DROP MATERIALIZED VIEW {ks}.{view_name}')
