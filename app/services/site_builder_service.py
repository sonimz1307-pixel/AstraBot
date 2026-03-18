from __future__ import annotations

import os
import zipfile
from io import BytesIO
from typing import Any, Dict, Optional

from app.services.site_builder_billing import refund_site_create, refund_site_revision
from app.services.site_builder_llm import (
    SITE_BUILDER_MODEL,
    apply_revision,
    build_blueprint,
    generate_css,
    generate_html,
    generate_js,
    generate_readme,
    normalize_brief,
)
from app.services.site_builder_repo import (
    JOB_CREATE,
    JOB_REVISION,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_GENERATING,
    create_version,
    get_job,
    get_latest_version,
    get_next_version_number,
    get_project,
    mark_free_revision_used,
    update_job,
    update_project,
)
from app.services.site_builder_storage import build_zip_storage_path, upload_zip

SITE_BUILDER_MODEL = (os.getenv("SITE_BUILDER_MODEL", SITE_BUILDER_MODEL) or SITE_BUILDER_MODEL).strip() or SITE_BUILDER_MODEL


class SiteBuildProcessError(RuntimeError):
    pass


def _ensure_text(raw: Optional[str], fallback: str) -> str:
    text = (raw or "").strip()
    return text or fallback


def _fallback_js() -> str:
    return '''document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll('[data-faq-question]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const item = btn.closest('[data-faq-item]');
      if (!item) return;
      item.classList.toggle('is-open');
    });
  });

  document.querySelectorAll('a[href^="#"]').forEach((link) => {
    link.addEventListener('click', (event) => {
      const href = link.getAttribute('href');
      if (!href || href === '#') return;
      const target = document.querySelector(href);
      if (!target) return;
      event.preventDefault();
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  });
});
'''


def _fallback_readme(version_number: int) -> str:
    return (
        f"Сайт готов. Это версия v{int(version_number)}.\n\n"
        "1. Откройте файл index.html в браузере.\n"
        "2. Для публикации загрузите весь архив на хостинг без изменения структуры файлов.\n"
        "3. styles.css отвечает за стили, script.js — за лёгкую интерактивность.\n"
    )


def _zip_site(*, html_content: str, css_content: str, js_content: str, readme_content: str) -> bytes:
    bio = BytesIO()
    with zipfile.ZipFile(bio, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("site/index.html", html_content)
        zf.writestr("site/styles.css", css_content)
        zf.writestr("site/script.js", js_content)
        zf.writestr("site/README.txt", readme_content)
    return bio.getvalue()


async def _render_site_files(*, structured_context: Dict[str, Any], blueprint: Dict[str, Any], version_number: int) -> Dict[str, str]:
    html_content = _ensure_text(
        await generate_html(structured_context=structured_context, blueprint=blueprint, model=SITE_BUILDER_MODEL),
        "",
    )
    if not html_content:
        raise SiteBuildProcessError("Model returned empty HTML")

    css_content = _ensure_text(
        await generate_css(
            structured_context=structured_context,
            blueprint=blueprint,
            html_content=html_content,
            model=SITE_BUILDER_MODEL,
        ),
        "",
    )
    if not css_content:
        raise SiteBuildProcessError("Model returned empty CSS")

    try:
        js_content = _ensure_text(
            await generate_js(blueprint=blueprint, html_content=html_content, model=SITE_BUILDER_MODEL),
            _fallback_js(),
        )
    except Exception:
        js_content = _fallback_js()

    try:
        readme_content = _ensure_text(
            await generate_readme(structured_context=structured_context, version_number=version_number, model=SITE_BUILDER_MODEL),
            _fallback_readme(version_number),
        )
    except Exception:
        readme_content = _fallback_readme(version_number)

    return {
        "html": html_content,
        "css": css_content,
        "js": js_content,
        "readme": readme_content,
    }


async def _process_create_job(job: Dict[str, Any]) -> Dict[str, Any]:
    project_id = str(job["project_id"])
    project = get_project(project_id)
    if not project:
        raise SiteBuildProcessError("Project not found")

    update_job(job["id"], {"status": STATUS_GENERATING})
    update_project(project_id, {"status": STATUS_GENERATING, "last_job_id": job["id"]})

    structured_context = await normalize_brief(
        brief_raw=str(project.get("brief_raw") or ""),
        extra_texts_raw=project.get("extra_texts_raw"),
        model=SITE_BUILDER_MODEL,
    )
    title = _ensure_text(structured_context.get("project_name"), str(project.get("title") or "Новый сайт"))
    blueprint = await build_blueprint(structured_context=structured_context, model=SITE_BUILDER_MODEL)
    version_number = get_next_version_number(project_id)
    files = await _render_site_files(structured_context=structured_context, blueprint=blueprint, version_number=version_number)
    zip_bytes = _zip_site(
        html_content=files["html"],
        css_content=files["css"],
        js_content=files["js"],
        readme_content=files["readme"],
    )
    storage_path = upload_zip(
        path=build_zip_storage_path(
            user_id=int(project["telegram_user_id"]),
            project_id=project_id,
            version_number=version_number,
            title=title,
        ),
        raw_bytes=zip_bytes,
    )
    version = create_version(
        project_id=project_id,
        version_number=version_number,
        source_type=JOB_CREATE,
        request_raw=str(project.get("brief_raw") or ""),
        blueprint_json=blueprint,
        html_content=files["html"],
        css_content=files["css"],
        js_content=files["js"],
        readme_content=files["readme"],
        zip_storage_path=storage_path,
    )
    update_project(
        project_id,
        {
            "status": STATUS_COMPLETED,
            "title": title,
            "brief_structured_json": structured_context,
            "current_version": version_number,
            "last_job_id": job["id"],
        },
    )
    update_job(job["id"], {"status": STATUS_COMPLETED})
    return {"project": get_project(project_id), "version": version, "zip_bytes": zip_bytes}


async def _process_revision_job(job: Dict[str, Any]) -> Dict[str, Any]:
    project_id = str(job["project_id"])
    revision_request = str(job.get("request_raw") or "").strip()
    if not revision_request:
        raise SiteBuildProcessError("Revision request is empty")

    project = get_project(project_id)
    if not project:
        raise SiteBuildProcessError("Project not found")

    current_version = get_latest_version(project_id)
    if not current_version:
        raise SiteBuildProcessError("Current version not found")

    update_job(job["id"], {"status": STATUS_GENERATING})
    update_project(project_id, {"status": STATUS_GENERATING, "last_job_id": job["id"]})

    structured_context = project.get("brief_structured_json") or await normalize_brief(
        brief_raw=str(project.get("brief_raw") or ""),
        extra_texts_raw=project.get("extra_texts_raw"),
        model=SITE_BUILDER_MODEL,
    )
    base_blueprint = current_version.get("blueprint_json") or await build_blueprint(structured_context=structured_context, model=SITE_BUILDER_MODEL)
    new_blueprint = await apply_revision(
        structured_context=structured_context,
        current_blueprint=base_blueprint,
        current_version=current_version,
        revision_request=revision_request,
        model=SITE_BUILDER_MODEL,
    )
    version_number = get_next_version_number(project_id)
    files = await _render_site_files(structured_context=structured_context, blueprint=new_blueprint, version_number=version_number)
    zip_bytes = _zip_site(
        html_content=files["html"],
        css_content=files["css"],
        js_content=files["js"],
        readme_content=files["readme"],
    )
    storage_path = upload_zip(
        path=build_zip_storage_path(
            user_id=int(project["telegram_user_id"]),
            project_id=project_id,
            version_number=version_number,
            title=str(project.get("title") or "site"),
        ),
        raw_bytes=zip_bytes,
    )
    version = create_version(
        project_id=project_id,
        version_number=version_number,
        source_type=JOB_REVISION,
        request_raw=revision_request,
        blueprint_json=new_blueprint,
        html_content=files["html"],
        css_content=files["css"],
        js_content=files["js"],
        readme_content=files["readme"],
        zip_storage_path=storage_path,
    )
    patch: Dict[str, Any] = {
        "status": STATUS_COMPLETED,
        "current_version": version_number,
        "last_job_id": job["id"],
    }
    if bool(job.get("is_free_revision")):
        patch["free_revision_used"] = True
    update_project(project_id, patch)
    if bool(job.get("is_free_revision")):
        mark_free_revision_used(project_id)
    update_job(job["id"], {"status": STATUS_COMPLETED})
    return {"project": get_project(project_id), "version": version, "zip_bytes": zip_bytes}


async def process_site_job(job_id: str) -> Dict[str, Any]:
    job = get_job(job_id)
    if not job:
        raise SiteBuildProcessError("Job not found")

    try:
        if str(job.get("job_type") or "") == JOB_CREATE:
            return await _process_create_job(job)
        if str(job.get("job_type") or "") == JOB_REVISION:
            return await _process_revision_job(job)
        raise SiteBuildProcessError(f"Unsupported job_type: {job.get('job_type')}")
    except Exception as exc:
        job = get_job(job_id) or job
        project_id = str(job.get("project_id") or "")
        user_id = int(job.get("telegram_user_id") or 0)
        update_job(job_id, {"status": STATUS_FAILED, "error_text": (str(exc) or "")[:4000]})
        if project_id:
            update_project(project_id, {"status": STATUS_FAILED})
        if user_id > 0:
            if str(job.get("job_type") or "") == JOB_CREATE and int(job.get("tokens_cost") or 0) > 0:
                refund_site_create(user_id=user_id, project_id=project_id, error=str(exc))
            elif str(job.get("job_type") or "") == JOB_REVISION and not bool(job.get("is_free_revision")) and int(job.get("tokens_cost") or 0) > 0:
                refund_site_revision(user_id=user_id, job_id=job_id, project_id=project_id, error=str(exc))
        raise
