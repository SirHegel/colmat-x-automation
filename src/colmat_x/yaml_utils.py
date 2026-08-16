from __future__ import annotations

from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver


class UniqueKeySafeLoader(yaml.SafeLoader):
    """SafeLoader que rechaza claves repetidas en cualquier nivel."""


def _construct_unique_mapping(
    loader: UniqueKeySafeLoader, node: MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "al construir un objeto",
                node.start_mark,
                "se encontró una clave no válida",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "al construir un objeto",
                node.start_mark,
                f"se encontró la clave duplicada {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeySafeLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_yaml_unique(text: str) -> Any:
    return yaml.load(text, Loader=UniqueKeySafeLoader)
