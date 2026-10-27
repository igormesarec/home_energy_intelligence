"""Optional read-only InfluxDB 1.x backfill client."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class InfluxBackfillError(RuntimeError):
    pass


class InfluxBackfillClient:
    """Minimal InfluxQL query client that cannot issue write requests."""

    def __init__(self, *, host: str, port: int, database: str,
                 username: str | None = None, password: str | None = None,
                 use_ssl: bool = False) -> None:
        self.base_url = f"{'https' if use_ssl else 'http'}://{host}:{port}/query"
        self.database = database
        self.username = username
        self.password = password

    def build_query(self, measurement: str, field: str, start: datetime,
                    end: datetime, *, entity_tag: str | None = None) -> str:
        if not measurement.replace("_", "").isalnum() or not field.replace("_", "").isalnum():
            raise InfluxBackfillError("measurement and field must be simple identifiers")
        if entity_tag is not None and not all(char.isalnum() or char in "_.-" for char in entity_tag):
            raise InfluxBackfillError("entity tag contains unsupported characters")
        tag_filter = f' AND "entity_id" = \'{entity_tag}\'' if entity_tag else ""
        return (
            f'SELECT "{field}" FROM "{measurement}" '
            f"WHERE time >= '{start.isoformat()}' AND time < '{end.isoformat()}'"
            f"{tag_filter} ORDER BY time ASC"
        )

    def query(self, measurement: str, field: str, start: datetime,
              end: datetime, *, entity_tag: str | None = None) -> list[tuple[datetime, float]]:
        """Run a read query; Home Assistant callers must use its executor."""
        return self._query(self.build_query(measurement, field, start, end, entity_tag=entity_tag))

    def _query(self, query: str) -> list[tuple[datetime, float]]:
        params = {"db": self.database, "q": query, "epoch": "ns"}
        if self.username:
            params["u"] = self.username
        if self.password:
            params["p"] = self.password
        request = Request(f"{self.base_url}?{urlencode(params)}", method="GET")
        with urlopen(request, timeout=30) as response:  # noqa: S310 - configured local endpoint
            payload = json.load(response)
        if payload.get("error"):
            raise InfluxBackfillError(payload["error"])
        try:
            values = payload["results"][0]["series"][0]["values"]
        except (KeyError, IndexError):
            return []
        return [(datetime.fromtimestamp(row[0] / 1_000_000_000, tz=UTC), float(row[1]))
                for row in values if row[1] is not None]
