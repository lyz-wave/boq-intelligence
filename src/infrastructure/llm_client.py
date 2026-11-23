from contextlib import contextmanager
import json
from pathlib import Path
import re
import threading
import time
from typing import Any, Iterator, Optional

import httpx
from pydantic import BaseModel, Field

from src.core.contracts.boq_item import FeatureDimension, FeatureSlotValue

DEFAULT_SETTINGS_PATH = Path(__file__).resolve().parents[2] / "data" / "llm_settings.json"


class LlmSettings(BaseModel):
    """大模型及第三方中转站持久化配置."""

    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = ""
    trigger_mode: str = "fallback"  # fallback: 仅在正则不足时兜底; always: 协同增强
    timeout_sec: float = 15.0
    available_models: list[str] = Field(default_factory=list)


def normalize_openai_base_url(url: str) -> str:
    """规范化任意 OpenAI 兼容中转站或本地服务的 Base URL."""
    cleaned = (url or "").strip()
    if not cleaned:
        return "https://api.openai.com/v1"
    if not cleaned.startswith(("http://", "https://")):
        cleaned = "https://" + cleaned
    cleaned = cleaned.rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/models"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].rstrip("/")
            break
    if re.search(r"/(v1|v1beta|v2|v3|v4|openai)$", cleaned, re.IGNORECASE):
        return cleaned
    return f"{cleaned}/v1"


def mask_api_key(key: str) -> str:
    """返回脱敏后的 API Key 展示串."""
    cleaned = (key or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) <= 8:
        return "****"
    return f"{cleaned[:4]}****{cleaned[-4:]}"


def _extract_model_ids(payload: Any) -> list[str]:
    """兼容 OpenAI / OneAPI / NewAPI / Ollama 等多格式模型列表响应."""
    ids: list[str] = []
    if isinstance(payload, dict):
        items = payload.get("data")
        if not isinstance(items, list):
            items = payload.get("models")
        if isinstance(items, list):
            for entry in items:
                if isinstance(entry, str) and entry.strip():
                    ids.append(entry.strip())
                elif isinstance(entry, dict):
                    mid = entry.get("id") or entry.get("name") or entry.get("model")
                    if isinstance(mid, str) and mid.strip():
                        ids.append(mid.strip())
    elif isinstance(payload, list):
        for entry in payload:
            if isinstance(entry, str) and entry.strip():
                ids.append(entry.strip())
            elif isinstance(entry, dict):
                mid = entry.get("id") or entry.get("name") or entry.get("model")
                if isinstance(mid, str) and mid.strip():
                    ids.append(mid.strip())
    return list(dict.fromkeys(ids))


def _parse_json_object(raw_text: str) -> dict[str, Any]:
    """从大模型返回文本中容错提取 JSON 对象（剥离 Markdown 代码块）."""
    text = (raw_text or "").strip()
    if not text:
        return {}
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        brace = re.search(r"\{[\s\S]*\}", text)
        if not brace:
            return {}
        try:
            obj = json.loads(brace.group(0))
            return obj if isinstance(obj, dict) else {}
        except json.JSONDecodeError:
            return {}


class LlmClientManager:
    """进程内单例大模型中转站管理器."""

    _instance: Optional["LlmClientManager"] = None
    _lock = threading.Lock()

    def __init__(self, settings_path: Path = DEFAULT_SETTINGS_PATH) -> None:
        self._settings_path = settings_path
        self._settings = self._load_from_disk()
        self._local = threading.local()

    @classmethod
    def get_instance(cls) -> "LlmClientManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset_for_tests(cls, settings_path: Optional[Path] = None) -> "LlmClientManager":
        with cls._lock:
            cls._instance = cls(settings_path=settings_path or DEFAULT_SETTINGS_PATH)
            return cls._instance

    @property
    def settings(self) -> LlmSettings:
        return self._settings

    @contextmanager
    def offline_scope(self) -> Iterator[None]:
        """在样本库冷启动与消融评测期间屏蔽远端 LLM 网络请求，保持确定性与高吞吐."""
        depth = getattr(self._local, "offline_depth", 0)
        self._local.offline_depth = depth + 1
        try:
            yield
        finally:
            self._local.offline_depth = depth

    def is_active(self) -> bool:
        if getattr(self._local, "offline_depth", 0) > 0:
            return False
        return bool(self._settings.enabled and self._settings.base_url.strip() and self._settings.model.strip())

    def _load_from_disk(self) -> LlmSettings:
        if not self._settings_path.exists():
            return LlmSettings()
        try:
            data = json.loads(self._settings_path.read_text(encoding="utf-8"))
            return LlmSettings(**data)
        except Exception:
            return LlmSettings()

    def _save_to_disk(self) -> None:
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        self._settings_path.write_text(
            self._settings.model_dump_json(indent=2),
            encoding="utf-8",
        )

    def get_public_view(self) -> dict[str, Any]:
        """返回不含明文 API Key 的前端安全配置视图."""
        return {
            "enabled": self._settings.enabled,
            "base_url": self._settings.base_url,
            "has_api_key": bool(self._settings.api_key),
            "api_key_masked": mask_api_key(self._settings.api_key),
            "model": self._settings.model,
            "trigger_mode": self._settings.trigger_mode,
            "timeout_sec": self._settings.timeout_sec,
            "available_models": list(self._settings.available_models),
        }

    def update_settings(
        self,
        *,
        enabled: Optional[bool] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        trigger_mode: Optional[str] = None,
        timeout_sec: Optional[float] = None,
        clear_api_key: bool = False,
        available_models: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        if enabled is not None:
            self._settings.enabled = enabled
        if base_url is not None and base_url.strip():
            self._settings.base_url = normalize_openai_base_url(base_url)
        if clear_api_key:
            self._settings.api_key = ""
        elif api_key is not None and api_key.strip():
            self._settings.api_key = api_key.strip()
        if model is not None:
            self._settings.model = model.strip()
        if trigger_mode in ("fallback", "always"):
            self._settings.trigger_mode = trigger_mode
        if timeout_sec is not None:
            self._settings.timeout_sec = max(3.0, min(120.0, float(timeout_sec)))
        if available_models is not None:
            self._settings.available_models = list(dict.fromkeys(m.strip() for m in available_models if m.strip()))
        self._save_to_disk()
        return self.get_public_view()

    def _build_headers(self, api_key: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if api_key.strip():
            headers["Authorization"] = f"Bearer {api_key.strip()}"
        return headers

    def fetch_models(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout_sec: Optional[float] = None,
    ) -> dict[str, Any]:
        """向中转站发起 GET /models 请求动态拉取模型列表."""
        raw_input = (base_url or self._settings.base_url).strip()
        if not raw_input:
            raise ValueError("请填写第三方中转站或模型服务的 Base URL。")
        norm_url = normalize_openai_base_url(raw_input)
        raw_stripped = raw_input if raw_input.startswith(("http://", "https://")) else f"https://{raw_input}"
        raw_stripped = raw_stripped.rstrip("/")

        use_key = api_key.strip() if (api_key and api_key.strip()) else self._settings.api_key
        use_timeout = timeout_sec or self._settings.timeout_sec

        candidates = [f"{norm_url}/models"]
        if f"{raw_stripped}/models" not in candidates:
            candidates.append(f"{raw_stripped}/models")

        last_err: Optional[str] = None
        models: list[str] = []
        used_endpoint = candidates[0]

        with httpx.Client(timeout=use_timeout, follow_redirects=True) as client:
            for endpoint in candidates:
                used_endpoint = endpoint
                resp = client.get(endpoint, headers=self._build_headers(use_key))
                if resp.status_code == 404 and endpoint != candidates[-1]:
                    last_err = f"404 on {endpoint}"
                    continue
                if resp.status_code >= 400:
                    detail = resp.text[:240].strip()
                    raise RuntimeError(f"中转站返回 HTTP {resp.status_code}: {detail}")
                payload = resp.json()
                models = _extract_model_ids(payload)
                break

        if not models and last_err:
            raise RuntimeError(f"未能从 {used_endpoint} 解析到模型列表 ({last_err})")

        self._settings.available_models = models
        if models and not self._settings.model:
            self._settings.model = models[0]
        if base_url and base_url.strip():
            self._settings.base_url = norm_url
        if api_key and api_key.strip():
            self._settings.api_key = api_key.strip()
        self._save_to_disk()

        return {
            "normalized_base_url": norm_url,
            "endpoint": used_endpoint,
            "models": models,
            "count": len(models),
        }

    def chat_extract_slots(
        self,
        text: str,
        domain: str = "",
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_sec: Optional[float] = None,
    ) -> dict[str, Any]:
        """调用 OpenAI 兼容 /chat/completions 接口按受控 Schema 抽取清单语义特征槽位."""
        norm_url = normalize_openai_base_url(base_url or self._settings.base_url)
        use_key = api_key.strip() if (api_key and api_key.strip()) else self._settings.api_key
        use_model = (model or self._settings.model).strip()
        use_timeout = timeout_sec or self._settings.timeout_sec

        if not use_model:
            raise ValueError("尚未选择或填写模型名称，请先拉取并选择模型。")

        sys_prompt = (
            "你是中国水利、生态环保、土地整治及安装工程造价领域的清单语义标准化专家。"
            "请从工程量清单描述中提取结构化特征槽位，仅输出合法 JSON，不要输出任何额外说明或 Markdown。"
            "JSON 格式严格如下："
            '{"slots":[{"slot_key":"英文蛇形键如material/spec_size/haul_distance_m/construction_method",'
            '"slot_label":"中文属性名","dimension":"entity_spec|process_work|env_condition",'
            '"normalized_value":"归一化值","numeric_value":纯数字或null,"hard_constraint":true或false}]}'
        )
        user_prompt = f"所属工程域: {domain or '自动识别'}\n清单描述文本: {text}"

        body = {
            "model": use_model,
            "temperature": 0.0,
            "max_tokens": 600,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        t0 = time.perf_counter()
        endpoint = f"{norm_url}/chat/completions"
        with httpx.Client(timeout=use_timeout, follow_redirects=True) as client:
            resp = client.post(endpoint, headers=self._build_headers(use_key), json=body)
            if resp.status_code >= 400:
                raise RuntimeError(f"中转站调用失败 HTTP {resp.status_code}: {resp.text[:260]}")
            data = resp.json()
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)

        choices = data.get("choices") or []
        content = ""
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            content = str(msg.get("content") or "")

        parsed = _parse_json_object(content)
        raw_slots = parsed.get("slots") if isinstance(parsed.get("slots"), list) else []
        slots: dict[str, FeatureSlotValue] = {}
        dim_map = {
            "entity_spec": FeatureDimension.ENTITY_SPEC,
            "process_work": FeatureDimension.PROCESS_WORK,
            "process_method": FeatureDimension.PROCESS_WORK,
            "env_condition": FeatureDimension.ENV_CONDITION,
            "environment": FeatureDimension.ENV_CONDITION,
        }
        for entry in raw_slots:
            if not isinstance(entry, dict):
                continue
            s_key = str(entry.get("slot_key") or "").strip()
            s_val = entry.get("normalized_value")
            if not s_key or s_val is None or str(s_val).strip() == "":
                continue
            num_val = entry.get("numeric_value")
            try:
                num_float = float(num_val) if num_val is not None else None
            except (TypeError, ValueError):
                num_float = None
            dim = dim_map.get(str(entry.get("dimension") or "").lower(), FeatureDimension.ENTITY_SPEC)
            slots[s_key] = FeatureSlotValue(
                slot_key=s_key,
                slot_label=str(entry.get("slot_label") or s_key),
                dimension=dim,
                raw_text=str(s_val),
                normalized_value=num_float if num_float is not None else str(s_val).strip(),
                numeric_value=num_float,
                hard_constraint=bool(entry.get("hard_constraint", False)),
                ahp_weight=0.20,
                extractor_source=f"llm:{use_model}",
            )

        return {
            "model": use_model,
            "endpoint": endpoint,
            "latency_ms": latency_ms,
            "slots": slots,
            "raw_preview": content[:320],
        }
