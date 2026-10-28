"""Optional read-only InfluxDB 1.x backfill client."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import json
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
            f"{tag_filter} ORDER BY time ASC LIMIT {self.max_points}"
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
            for timestamp, value in self._query(
                self.build_query(measurement, field, cursor, chunk_end, entity_tag=entity_tag)
            ):
                rows[timestamp] = value
            if len(rows) > self.max_points:
                raise InfluxBackfillError("historical query exceeded configured point limit")
            cursor = chunk_end
        return sorted(rows.items())

    def _query(self, query: str) -> list[tuple[datetime, float]]:
        params = {"db": self.database, "q": query, "epoch": "ns"}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if self.username:
            token = base64.b64encode(f"{self.username}:{self.password or ''}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        request = Request(self.base_url, data=urlencode(params).encode(), headers=headers, method="POST")
        context = None if self.verify_ssl else ssl._create_unverified_context()  # noqa: SLF001
        with urlopen(request, timeout=30, context=context) as response:  # noqa: S310 - configured endpoint
            payload = json.load(response)
        if payload.get("error"):
            raise InfluxBackfillError(payload["error"])
        try:
            values = payload["results"][0]["series"][0]["values"]
        except (KeyError, IndexError):
            return []
        return [(datetime.fromtimestamp(row[0] / 1_000_000_000, tz=UTC), float(row[1]))
                for row in values if row[1] is not None]
