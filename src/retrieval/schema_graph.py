from __future__ import annotations

from collections import deque
from typing import Any


def shortest_path(
    schema: dict[str, dict[str, Any]], start: str, target: str
) -> list[str]:
    if start == target:
        return [start]
    graph: dict[str, set[str]] = {name: set() for name in schema}
    for table_name, info in schema.items():
        for fk in info.get("foreign_keys", []) or []:
            other = str(fk.get("referenced_table") or "")
            if other in graph:
                graph[table_name].add(other)
                graph[other].add(table_name)
    queue = deque([(start, [start])])
    visited = {start}
    while queue:
        node, path = queue.popleft()
        for neighbor in graph.get(node, ()):
            if neighbor == target:
                return [*path, neighbor]
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, [*path, neighbor]))
    return []


def bridge_tables(
    schema: dict[str, dict[str, Any]], selected_tables: list[str]
) -> list[str]:
    bridges: list[str] = []
    for index, left in enumerate(selected_tables):
        for right in selected_tables[index + 1 :]:
            for table in shortest_path(schema, left, right)[1:-1]:
                if table not in selected_tables and table not in bridges:
                    bridges.append(table)
    return bridges
