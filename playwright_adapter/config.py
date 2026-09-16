import os
from dataclasses import dataclass, replace
from pathlib import Path


PROJECT_DIR = str(Path(__file__).resolve().parent.parent)
DEFAULT_URL = os.getenv("ZHIHU_URL", "https://www.zhihu.com/people/li-xiang-57-76")
PAGE_LOAD_TIMEOUT_MS = 30000


@dataclass(frozen=True)
class RuntimeConfig:
    project_dir: str
    default_url: str
    archive_root_dir: str
    local_archive_root_dir: str
    db_file: str
    state_file: str
    page_load_timeout_ms: int = PAGE_LOAD_TIMEOUT_MS


def default_runtime_config() -> RuntimeConfig:
    archive_root = os.getenv(
        "ARCHIVE_ROOT_DIR",
        os.path.join(PROJECT_DIR, "data", "articles"),
    )
    return RuntimeConfig(
        project_dir=PROJECT_DIR,
        default_url=DEFAULT_URL,
        archive_root_dir=archive_root,
        local_archive_root_dir=os.getenv("LOCAL_ARCHIVE_ROOT_DIR", archive_root),
        db_file=os.getenv("ZH_DB_FILE", os.path.join(PROJECT_DIR, "zhihu_articles.db")),
        state_file=os.getenv("ZHIHU_STATE_FILE", os.path.join(PROJECT_DIR, "state.json")),
    )

def with_runtime_overrides(
    config: RuntimeConfig,
    *,
    output_dir: str | None = None,
    db_file: str | None = None,
    state_file: str | None = None,
) -> RuntimeConfig:
    archive_root = os.path.abspath(output_dir) if output_dir else config.archive_root_dir
    return replace(
        config,
        archive_root_dir=archive_root,
        local_archive_root_dir=archive_root if output_dir else config.local_archive_root_dir,
        db_file=os.path.abspath(db_file) if db_file else config.db_file,
        state_file=os.path.abspath(state_file) if state_file else config.state_file,
    )
