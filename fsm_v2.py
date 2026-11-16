"""
FSM v2: DAG-based pipeline executor.
Reads pipeline.json, executes opencode + reviewer stages, handles artifacts.
Supports user comments injection between iterations and SSE notifications.
"""
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import md2docx
from chat_driver import ChatDriver
from opencode_driver import opencode_run, wait_for_done
from prompt_builder import (
    build_prompt, build_reviewer_prompt, build_opencode_prompt,
    load_user_comments, mark_comments_injected,
)

BASE_DIR = Path(__file__).parent
SESSIONS_DIR = BASE_DIR / "sessions"
CONFIG_PATH = BASE_DIR / "config.json"
PIPELINE_PATH = BASE_DIR / "pipeline.json"
JOURNAL_PATH = BASE_DIR / "journal.jsonl"
REGISTRY_PATH = SESSIONS_DIR / "registry.json"


def _update_registry(session_dir: Path, status: str, detail: dict):
    """Track the session in registry.json so the UI can find and return to it."""
    reg = {}
    if REGISTRY_PATH.exists():
        try:
            reg = json.loads(REGISTRY_PATH.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, OSError):
            reg = {}
    prev = (reg.get("sessions") or {}).get(session_dir.name, {})
    entry = {
        "updated": datetime.now().isoformat(),
        "status": status,
        "detail": detail,
    }
    pid = prev.get("project_id")
    if not pid:
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
            pid = cfg.get("active_project_id")
        except (json.JSONDecodeError, OSError):
            pid = None
    if pid:
        entry["project_id"] = pid
    reg.setdefault("sessions", {})[session_dir.name] = entry
    REGISTRY_PATH.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_status(session_dir: Path, status: str, detail: dict):
    """Write status.json into the session dir (read by the dashboard)."""
    save_json(session_dir / "status.json", {"status": status, "detail": detail})


def load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def append_journal(entry: dict):
    with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def create_session(task_name: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = task_name.replace(" ", "_")[:40]
    session_dir = SESSIONS_DIR / f"{slug}_{ts}"
    for sub in ["spec", "prototyping", "prep", "reviews", "analysis", "metrics"]:
        (session_dir / sub).mkdir(parents=True, exist_ok=True)
    return session_dir


def resolve_inputs(inputs: list[str], artifacts: dict[str, str]) -> str:
    parts = []
    for inp in inputs:
        path = artifacts.get(inp)
        if path and Path(path).exists():
            text = Path(path).read_text(encoding="utf-8")
            parts.append(f"=== {inp} ===\n{text}")
    return "\n\n".join(parts)


class PipelineExecutor:
    def __init__(self, task: str, pipeline_name: str = "default-2-reviewers"):
        self.task = task
        self.pipeline = load_json(PIPELINE_PATH)
        self.config = load_json(CONFIG_PATH)
        self.session_dir = create_session(task)
        self.artifacts: dict[str, str] = {}
        self.stage_results: dict[str, Any] = {}
        self.chat: Optional[ChatDriver] = None
        self._iteration = 0

    async def run(self):
        print(f"\n{'='*60}")
        print(f"Pipeline: {self.pipeline['name']}")
        print(f"Task: {self.task}")
        print(f"Session: {self.session_dir}")
        print(f"{'='*60}\n")

        append_journal({
            "event": "pipeline_start",
            "task": self.task,
            "session": str(self.session_dir),
            "pipeline": self.pipeline["name"],
            "timestamp": time.time(),
        })

        self.artifacts["task"] = self.task

        # Register the session + write initial status so the UI can see it
        n_iters = self.pipeline.get("iterations", 2)
        _write_status(self.session_dir, "running", {"iter": 0, "N": n_iters})
        _update_registry(self.session_dir, "running", {"iter": 0, "N": n_iters})

        # Pre-open tabs for all services this pipeline needs
        stages = self.pipeline["stages"]
        required_services = set()
        for stage in stages:
            svc = stage.get("service", "")
            if svc and svc != "opencode" and svc != "local":
                required_services.add(svc)
        if required_services:
            if not self.chat:
                self.chat = ChatDriver(headless=False)
                await self.chat.start()
            await self.chat.pre_open_tabs(list(required_services))
            print(f"[fsm] Pre-opened tabs: {list(required_services)}")
        groups: dict[str, list[dict]] = {}
        for stage in stages:
            g = stage.get("parallel_group")
            if g:
                groups.setdefault(g, []).append(stage)
            else:
                groups[stage["id"]] = [stage]

        executed = set()
        for stage in stages:
            sid = stage["id"]
            if sid in executed:
                continue
            pg = stage.get("parallel_group")
            if pg and pg in groups:
                group_stages = groups.pop(pg)
                await self._execute_parallel(group_stages, executed)
            elif not pg:
                await self._execute_stage(stage, executed)

        self._save_metrics()
        # Unlock all pages when pipeline finishes
        if self.chat:
            await self.chat.unlock_all()
        # Mark session done in status + registry
        total_s = sum(r.get("elapsed", 0) for r in self.stage_results.values())
        n_iters = self.pipeline.get("iterations", 2)
        done_detail = {"iter": self._iteration, "N": n_iters, "total_s": round(total_s, 1)}
        _write_status(self.session_dir, "done", done_detail)
        _update_registry(self.session_dir, "done", done_detail)
        append_journal({"event": "session_done", "session": str(self.session_dir), "timestamp": time.time()})
        print(f"\nPipeline complete. Session: {self.session_dir}")
        return self.session_dir

    async def _execute_parallel(self, stages: list[dict], executed: set):
        print(f"\n--- Parallel group: {stages[0].get('parallel_group')} ---")
        tasks = []
        for stage in stages:
            tasks.append(self._execute_stage(stage, executed))
        await asyncio.gather(*tasks)

    async def _execute_stage(self, stage: dict, executed: set):
        sid = stage["id"]
        stype = stage["type"]
        role = stage.get("role", "")
        prompt_template = stage.get("prompt", "")
        inputs = stage.get("inputs", [])
        outputs = stage.get("outputs", [])
        artifact_dir = stage.get("artifact_dir", "")

        self._iteration += 1
        print(f"\n>>> Stage: {sid} (type={stype}, role={role}, iter={self._iteration})")
        t0 = time.time()

        resolved_inputs = resolve_inputs(inputs, self.artifacts)

        # Load and inject user comments for this iteration
        comments = load_user_comments(str(self.session_dir), self._iteration)
        user_comments_text = ""
        if comments:
            user_comments_text = "\n\n".join(
                f"- [{c.get('created','?')}] {c['text']}" for c in comments
            )
            print(f"  📝 Injecting {len(comments)} user comment(s)")

        full_prompt = build_prompt(
            role_name=role,
            task_description=self.task,
            artifacts_text=resolved_inputs,
            user_comments=user_comments_text,
        )

        full_prompt = prompt_template + "\n\n" + full_prompt if prompt_template else full_prompt

        if stype == "opencode":
            result = await self._run_opencode(sid, full_prompt, outputs, artifact_dir)
        elif stype == "reviewer":
            svc = stage.get("service", "")
            # Lock page during reviewer execution
            if self.chat and svc in ("zai", "deepseek", "qwen", "google"):
                await self.chat.new_chat(svc)
                await self.chat.lock_page(svc)
            try:
                result = await self._run_reviewer(sid, stage, full_prompt, outputs, artifact_dir)
            finally:
                # Always unlock after reviewer completes (or fails)
                if self.chat and svc in ("zai", "deepseek", "qwen", "google"):
                    await self.chat.unlock_page(svc)
        else:
            raise ValueError(f"Unknown stage type: {stype}")

        elapsed = time.time() - t0
        self.stage_results[sid] = {"elapsed": elapsed, "status": "ok"}
        executed.add(sid)

        # Mark injected comments
        if comments:
            mark_comments_injected(str(self.session_dir), [c["id"] for c in comments])

        # Notify SSE clients
        try:
            from ui.app import notify_log, notify_stage
            notify_stage(sid, "done", f"{elapsed:.1f}s")
        except ImportError:
            pass

        append_journal({
            "event": "stage_done",
            "stage": sid,
            "elapsed": round(elapsed, 1),
            "timestamp": time.time(),
        })

        print(f"<<< Stage {sid} done in {elapsed:.1f}s")

    async def _run_opencode(self, sid: str, prompt: str, outputs: list, artifact_dir: str) -> str:
        out_dir = self.session_dir / artifact_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        target = self.config.get("target_project")
        mode = self.config.get("target_mode") or "scratch"
        extra = f"\n\nРезультат сохрани в: {out_dir}"
        if mode == "project" and target:
            extra += (
                f"\nКод и правки пиши в существующий проект: {target}. "
                "Не размазывай файлы по папке сессии Qmeka."
            )
        else:
            extra += (
                "\nПроекта ещё нет — эскизы клади в подпапку prototyping этой сессии."
            )
        full_prompt = prompt + extra
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: opencode_run(full_prompt, cwd=str(out_dir))
        )
        result_path = out_dir / f"{sid}.md"
        result_path.write_text(result, encoding="utf-8")
        self.artifacts[f"{sid}.md"] = str(result_path)

        if "plan.md" in outputs or "plan.docx" in outputs:
            docx_path = out_dir / f"{sid}.docx"
            md2docx.ensure_docx(str(result_path), str(docx_path))
            self.artifacts[f"{sid}.docx"] = str(docx_path)

        for out in outputs:
            if out not in self.artifacts:
                candidate = out_dir / out
                if candidate.exists():
                    self.artifacts[out] = str(candidate)

        return result

    async def _run_reviewer(self, sid: str, stage: dict, prompt: str, outputs: list, artifact_dir: str) -> str:
        service = stage.get("service", "zai")
        role = stage.get("role", "")
        thinking = stage.get("thinking")

        out_dir = self.session_dir / artifact_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        plan_text = ""
        for inp in stage.get("inputs", []):
            p = self.artifacts.get(inp)
            if p and Path(p).exists():
                plan_text += Path(p).read_text(encoding="utf-8") + "\n\n"

        reviewer_prompt = build_reviewer_prompt(
            service_name=service,
            role_name=role,
            plan_text=plan_text,
        )

        if thinking == "deepthink":
            reviewer_prompt = "/think deep\n\n" + reviewer_prompt

        docx_artifact = None
        for inp in stage.get("inputs", []):
            if inp.endswith(".docx"):
                p = self.artifacts.get(inp)
                if p:
                    docx_artifact = p
                    break

        if not self.chat:
            self.chat = ChatDriver(headless=False)
            await self.chat.start()

        response = await self.chat.run_prompt(
            service_name=service,
            prompt=reviewer_prompt,
            docx_path=docx_artifact,
            timeout=300,
        )

        result_path = out_dir / f"{sid}.md"
        result_path.write_text(response, encoding="utf-8")
        self.artifacts[f"{sid}.md"] = str(result_path)
        for out in outputs:
            if out not in self.artifacts:
                candidate = out_dir / out
                if candidate.exists():
                    self.artifacts[out] = str(candidate)

        return response

    def _save_metrics(self):
        metrics = {
            "task": self.task,
            "pipeline": self.pipeline["name"],
            "session": str(self.session_dir),
            "stages": self.stage_results,
            "artifacts": self.artifacts,
            "total_time": sum(r.get("elapsed", 0) for r in self.stage_results.values()),
            "timestamp": time.time(),
        }
        save_json(self.session_dir / "metrics" / "metrics.json", metrics)


async def main():
    if len(sys.argv) < 2:
        print("Usage: python fsm_v2.py <task description>")
        sys.exit(1)
    task = " ".join(sys.argv[1:])
    executor = PipelineExecutor(task)
    try:
        await executor.run()
    except Exception as e:
        n_iters = executor.pipeline.get("iterations", 2)
        _write_status(executor.session_dir, "failed", {"iter": executor._iteration, "N": n_iters, "error": str(e)})
        _update_registry(executor.session_dir, "failed", {"iter": executor._iteration, "N": n_iters, "error": str(e)})
        append_journal({"event": "session_failed", "session": str(executor.session_dir), "error": str(e), "timestamp": time.time()})
        raise


if __name__ == "__main__":
    asyncio.run(main())
