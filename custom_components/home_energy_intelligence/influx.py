"""Optional read-only InfluxDB 1.x backfill client."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import json
from math import isfinite
import ssl
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class InfluxBackfillError(RuntimeError):
    pass


class InfluxBackfillClient:
    """Minimal InfluxQL query client that cannot issue write requests."""

    def __init__(self, *, host: str, port: int, database: str,
                 username: str | None = None, password: str | None = None,
                 use_ssl: bool = False, verify_ssl: bool = True,
                 retention_policy: str = "autogen", max_points: int = 100_000) -> None:
        self.base_url = f"{'https' if use_ssl else 'http'}://{host}:{port}/query"
        self.database = database
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.retention_policy = retention_policy
        self.max_points = max_points

    @staticmethod
    def quote_identifier(value: str) -> str:
        if not value or "\x00" in value:
            raise InfluxBackfillError("identifier is empty or contains NUL")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    @staticmethod
    def quote_string(value: str) -> str:
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"

    def build_query(self, measurement: str, field: str, start: datetime,
                    end: datetime, *, entity_tag: str | None = None) -> str:
        measurement_ref = f"{self.quote_identifier(self.retention_policy)}.{self.quote_identifier(measurement)}"
        tag_filter = f' AND "entity_id" = {self.quote_string(entity_tag)}' if entity_tag else ""
        return (
            f'SELECT {self.quote_identifier(field)} FROM {measurement_ref} '
            f"WHERE time >= '{start.isoformat()}' AND time < '{end.isoformat()}'"
            f"{tag_filter} ORDER BY time ASC LIMIT {self.max_points + 1}"
        )

    def query(self, measurement: str, field: str, start: datetime,
              end: datetime, *, entity_tag: str | None = None) -> list[tuple[datetime, float]]:
        """Run a read query; Home Assistant callers must use its executor."""
        if end <= start or end - start > timedelta(days=31):
            raise InfluxBackfillError("InfluxDB query must be bounded to 31 days or less")
        rows: dict[datetime, float] = {}
        cursor = start
        while cursor < end:
            chunk_end = min(end, cursor + timedelta(days=1))
            chunk = self._query(
                self.build_query(measurement, field, cursor, chunk_end, entity_tag=entity_tag)
            )
            if len(chunk) > self.max_points:
                raise InfluxBackfillError("historical query exceeded configured point limit")
            for timestamp, value in chunk:
                if timestamp in rows and rows[timestamp] != value:
                    raise InfluxBackfillError("conflicting numeric timestamps")
                rows[timestamp] = value
            if len(rows) > self.max_points:
                raise InfluxBackfillError("historical query exceeded configured point limit")
            cursor = chunk_end
        return sorted(rows.items())

    def _query(self, query: str) -> list[tuple[datetime, float]]:
        return [(timestamp, float(value)) for timestamp, value in self._query_values(query)
                if value is not None]

    def _query_values(self, query: str) -> list[tuple[datetime, object]]:
        params = {"db": self.database, "q": query, "epoch": "ns"}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if self.username:
            token = base64.b64encode(f"{self.username}:{self.password or ''}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        request = Request(self.base_url, data=urlencode(params).encode(), headers=headers, method="POST")
        context = None if self.verify_ssl else ssl._create_unverified_context()  # noqa: SLF001
        with urlopen(request, timeout=30, context=context) as response:  # noqa: S310 - configured endpoint
            payload = json.load(response)
        if not isinstance(payload, dict):
            raise InfluxBackfillError("malformed InfluxDB response")
        if payload.get("error"):
            raise InfluxBackfillError("InfluxDB query failed")
        if any(result.get("error") for result in payload.get("results", [])):
            raise InfluxBackfillError("InfluxDB query failed")
        try:
            results = payload["results"]
            if not isinstance(results, list):
                raise ValueError("invalid result")
            values = [row for result in results for series in result.get("series", [])
                      for row in series["values"]]
            return [(datetime.fromtimestamp(row[0] / 1_000_000_000, tz=UTC), row[1])
                    for row in values if row[1] is not None]
        except (KeyError, IndexError, TypeError, ValueError, OverflowError) as error:
            raise InfluxBackfillError("malformed InfluxDB response") from error

    def query_states(self, start: datetime, end: datetime, *, entity_id: str,
                     domain: str, maximum_age: timedelta = timedelta(hours=1)) -> list[tuple[datetime, str]]:
        """Read bounded text state changes and at most one fresh preceding state."""
        if (start.tzinfo is None or end.tzinfo is None or end <= start
                or end - start > timedelta(days=31) or maximum_age <= timedelta(0)
                or maximum_age > timedelta(days=1) or not entity_id or not domain):
            raise InfluxBackfillError("state query requires a valid bounded interval and source")
        tag = entity_id.split(".", 1)[1] if entity_id.startswith(domain + ".") else entity_id
        where = f'"entity_id" = {self.quote_string(tag)} AND "domain" = {self.quote_string(domain)}'
        measurement = f'{self.quote_identifier(self.retention_policy)}.{self.quote_identifier("state")}'
        before = (f'SELECT "state" FROM {measurement} WHERE time >= {self.quote_string((start-maximum_age).isoformat())} '
                  f'AND time < {self.quote_string(start.isoformat())} AND {where} ORDER BY time DESC LIMIT 1')
        rows: dict[datetime, str] = {}
        def add(items, lower, upper):
            for stamp, value in items:
                if not isinstance(stamp, datetime) or stamp.tzinfo is None or not lower <= stamp < upper or not isinstance(value, str):
                    raise InfluxBackfillError("malformed state response")
                if stamp in rows and rows[stamp] != value:
                    raise InfluxBackfillError("conflicting state timestamps")
                rows[stamp] = value
                if len(rows) > self.max_points:
                    raise InfluxBackfillError("historical query exceeded configured point limit")
        add(self._query_values(before), start-maximum_age, start)
        cursor = start
        while cursor < end:
            stop = min(end, cursor+timedelta(days=1))
            query = (f'SELECT "state" FROM {measurement} WHERE time >= {self.quote_string(cursor.isoformat())} '
                     f'AND time < {self.quote_string(stop.isoformat())} AND {where} ORDER BY time ASC LIMIT {self.max_points+1}')
            chunk = self._query_values(query)
            if len(chunk) > self.max_points:
                raise InfluxBackfillError("historical query exceeded configured point limit")
            add(chunk, cursor, stop)
            cursor = stop
        return sorted(rows.items())

    def query_counter(self, measurement: str, field: str, start: datetime, end: datetime,
                      *, entity_tag: str, maximum_age: timedelta = timedelta(hours=1)) -> list[tuple[datetime, float]]:
        """Read a counter range with one bounded preceding baseline, if available."""
        if (start.tzinfo is None or end.tzinfo is None or end <= start
                or end-start > timedelta(days=31) or maximum_age <= timedelta(0)
                or maximum_age > timedelta(days=1) or not entity_tag):
            raise InfluxBackfillError("counter query requires a valid bounded interval and source")
        ref = f"{self.quote_identifier(self.retention_policy)}.{self.quote_identifier(measurement)}"
        query = (f'SELECT {self.quote_identifier(field)} FROM {ref} '
                 f'WHERE time >= {self.quote_string((start-maximum_age).isoformat())} '
                 f'AND time < {self.quote_string(start.isoformat())} '
                 f'AND "entity_id" = {self.quote_string(entity_tag)} ORDER BY time DESC LIMIT 1')
        before = self._query(query)
        if len(before) > 1 or any(stamp.tzinfo is None or not start-maximum_age <= stamp < start
                                  or not isfinite(value) or value < 0 for stamp, value in before):
            raise InfluxBackfillError("malformed counter baseline")
        rows = self.query(measurement, field, start, end, entity_tag=entity_tag)
        if len(before)+len(rows) > self.max_points:
            raise InfluxBackfillError("historical query exceeded configured point limit")
        return before + rows
