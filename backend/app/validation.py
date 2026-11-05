"""Strict HTTP input validation before invoking the simulation domain."""

from __future__ import annotations

import math
from typing import Any


def integer(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer between {low} and {high}.")
    try:
        result = int(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{name} must be an integer between {low} and {high}.") from None
    if str(value).strip() != str(result) or not low <= result <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}.")
    return result


def number(value: Any, name: str, low: float = 0, high: float = 10000) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number between {low} and {high}.")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{name} must be a finite number between {low} and {high}.") from None
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{name} must be a finite number between {low} and {high}.")
    return result


def identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value.strip()) > 64 or not value.strip():
        raise ValueError(f"{name} must be a location ID.")
    return value.strip().upper()


def scenario(data: Any, graph: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object.")
    if not isinstance(data.get("orders"), list) or not 1 <= len(data["orders"]) <= 60:
        raise ValueError("Add between 1 and 60 orders.")
    if not isinstance(data.get("drivers"), list) or not 1 <= len(data["drivers"]) <= 30:
        raise ValueError("Add between 1 and 30 driver starting points.")
    clean = {
        "orders": [], "drivers": [],
        "seed": integer(data.get("seed", 42), "seed", 0, 1_000_000),
        "capacity": integer(data.get("capacity", 3), "capacity", 1, 10),
        "algorithm": data.get("algorithm", "tree"),
        "metric": data.get("metric", "time"),
        "improve": data.get("improve", "relocate"),
        "closures": [],
        "enforce_serviceability": True,
    }
    if clean["algorithm"] not in ("dijkstra", "astar", "tree"):
        raise ValueError("algorithm must be dijkstra, astar or tree.")
    if clean["metric"] not in ("time", "distance"):
        raise ValueError("metric must be time or distance.")
    if clean["improve"] not in ("none", "intra", "relocate"):
        raise ValueError("improve must be none, intra or relocate.")
    extra = data.get("max_ride_extra_min", 20)
    clean["max_ride_extra_min"] = None if extra is None else number(extra, "max_ride_extra_min", 0, 120)
    if "enforce_serviceability" in data and data["enforce_serviceability"] is not True:
        raise ValueError("Serviceability cannot be disabled for API scenarios.")

    for i, raw in enumerate(data["orders"], 1):
        if not isinstance(raw, dict):
            raise ValueError(f"Order {i} must be an object.")
        restaurant = identifier(raw.get("restaurant"), f"Order {i} restaurant")
        society = identifier(raw.get("society"), f"Order {i} society")
        if graph.locations.get(restaurant) is None or graph.locations[restaurant].kind != "Restaurant":
            raise ValueError(f"Order {i}: unknown restaurant {restaurant}.")
        if graph.locations.get(society) is None or graph.locations[society].kind != "Society":
            raise ValueError(f"Order {i}: unknown society {society}.")
        item = {"restaurant": restaurant, "society": society}
        for field in ("ready_at", "deadline"):
            if raw.get(field) is not None and raw.get(field) != "":
                item[field] = number(raw[field], f"Order {i} {field}", 0, 10000)
        clean["orders"].append(item)

    for i, raw in enumerate(data["drivers"], 1):
        node = identifier(raw, f"Driver {i} start")
        if node not in graph.locations:
            raise ValueError(f"Driver {i}: unknown location {node}.")
        clean["drivers"].append(node)

    closures = data.get("closures", [])
    if not isinstance(closures, list) or len(closures) > 12:
        raise ValueError("At most 12 directed road closures are allowed.")
    seen = set()
    for i, raw in enumerate(closures, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"Closure {i} must be an object.")
        source = identifier(raw.get("source"), f"Closure {i} source")
        target = identifier(raw.get("target"), f"Closure {i} target")
        if not graph.has_edge(source, target):
            raise ValueError(f"No directed road {source} → {target} exists.")
        if (source, target) in seen:
            raise ValueError("Each directed road can be closed only once per scenario.")
        seen.add((source, target))
        at = number(raw.get("at", 0), f"Closure {i} at", 0, 10000)
        event = {"source": source, "target": target, "at": at}
        if raw.get("reopen_at") is not None and raw.get("reopen_at") != "":
            event["reopen_at"] = number(raw["reopen_at"], f"Closure {i} reopen_at", at + 0.001, 10000)
        clean["closures"].append(event)
    return clean
