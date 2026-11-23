from typing import Any, Callable, TypeVar

T = TypeVar("T")


class PluginRegistry:
    """微内核插件注册表：支持按接缝类别动态注册、按ID查询和自动发现."""

    _stores: dict[str, dict[str, type[Any]]] = {}

    @classmethod
    def register(cls, category: str, plugin_id: str | None = None) -> Callable[[type[T]], type[T]]:
        def decorator(plugin_cls: type[T]) -> type[T]:
            bucket = cls._stores.setdefault(category, {})
            key = plugin_id or getattr(plugin_cls, "parser_id", None) or getattr(
                plugin_cls, "extractor_id", None
            ) or getattr(plugin_cls, "retriever_id", None) or getattr(
                plugin_cls, "reranker_id", None
            ) or getattr(plugin_cls, "explainer_id", None) or getattr(
                plugin_cls, "adjuster_id", None
            ) or getattr(plugin_cls, "rule_id", None) or plugin_cls.__name__
            bucket[str(key)] = plugin_cls
            return plugin_cls

        return decorator

    @classmethod
    def get(cls, category: str, plugin_id: str) -> type[Any]:
        bucket = cls._stores.get(category, {})
        if plugin_id not in bucket:
            raise KeyError(f"Plugin '{plugin_id}' not found in category '{category}'. Available: {list(bucket.keys())}")
        return bucket[plugin_id]

    @classmethod
    def all_in(cls, category: str) -> dict[str, type[Any]]:
        return dict(cls._stores.get(category, {}))

    @classmethod
    def instantiate_all(cls, category: str) -> list[Any]:
        instances = [plugin_cls() for plugin_cls in cls.all_in(category).values()]
        instances.sort(key=lambda obj: getattr(obj, "priority", 100))
        return instances
