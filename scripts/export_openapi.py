"""导出 OpenAPI schema 到 ``docs/openapi.json``。

用法::

    uv run python scripts/export_openapi.py

导出后可用 swagger-ui / redoc / Apifox 等打开查看，或交付给前端。
"""

from __future__ import annotations

import json
from pathlib import Path

from twilightcupbackend.main import app


def main() -> None:
    spec = app.openapi()
    out = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    n_paths = len(spec["paths"])
    n_ops = sum(len(methods) for methods in spec["paths"].values())
    print(f"已导出 OpenAPI {spec['openapi']} → {out}（{n_paths} 路径 / {n_ops} 操作）")


if __name__ == "__main__":
    main()
