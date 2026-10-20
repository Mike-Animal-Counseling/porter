"""Owner workflow for previewing and carrying context between tools."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from .i18n import get_lang, t
from .panel import _copy_btn, _deps, _layout, esc, panel_owner
from .switch import build_handoff, render_briefing

router = APIRouter(prefix="/panel/handoff", dependencies=[Depends(panel_owner)])
_PRIVATE = {"Cache-Control": "no-store"}


async def _page(request, *, project="", from_harness="codex", to_harness="cursor", briefing=""):
    lang = get_lang()
    repo, _, _ = _deps(request)
    recent = await repo.list(status="active", limit=500)
    projects = sorted(
        {tag[8:] for r in recent for tag in r.scope_tags if tag.startswith("project:")}
    )
    suggestions = "".join(f'<option value="{esc(p)}"></option>' for p in projects)
    result = ""
    if briefing:
        result = f"""<section class="card" aria-labelledby="briefing-title">
<div class="row"><h2 id="briefing-title">{esc(t("handoff_result", lang))}</h2>
{_copy_btn(briefing, lang)}</div>
<textarea rows="20" readonly aria-label="{esc(t("handoff_result", lang))}">{esc(briefing)}</textarea>
</section>"""
    body = f"""<div class="card page--narrow">
<p class="card__sub">{esc(t("handoff_intro", lang))}</p>
<form method="post" action="/panel/handoff">
<label class="f" for="handoff-project">{esc(t("handoff_project", lang))}</label>
<input id="handoff-project" name="project" value="{esc(project)}" list="handoff-projects" maxlength="128" required>
<datalist id="handoff-projects">{suggestions}</datalist>
<label class="f" for="handoff-from">{esc(t("handoff_from", lang))}</label>
<input id="handoff-from" name="from_harness" value="{esc(from_harness)}" maxlength="64" list="handoff-tools" required>
<label class="f" for="handoff-to">{esc(t("handoff_to", lang))}</label>
<input id="handoff-to" name="to_harness" value="{esc(to_harness)}" maxlength="64" list="handoff-tools" required>
<datalist id="handoff-tools"><option value="codex"></option><option value="claude-code"></option><option value="cursor"></option></datalist>
<p class="small muted">{esc(t("handoff_privacy", lang))}</p>
<div class="row mt"><button class="btn btn--primary" type="submit">{esc(t("handoff_preview", lang))}</button>
<button class="btn btn--secondary" type="submit" formaction="/panel/handoff/download">{esc(t("handoff_download", lang))}</button></div>
</form></div>{result}"""
    return HTMLResponse(
        await _layout(request, "handoff", t("nav_handoff", lang), body, lang), headers=_PRIVATE
    )


@router.get("")
async def handoff_page(request: Request):
    return await _page(request)


@router.post("")
async def handoff_preview(
    request: Request,
    project: str = Form(min_length=1, max_length=128),
    from_harness: str = Form(min_length=1, max_length=64),
    to_harness: str = Form(min_length=1, max_length=64),
):
    repo, _, _ = _deps(request)
    payload = await build_handoff(
        repo,
        project=project,
        from_harness=from_harness,
        to_harness=to_harness,
        top_k=20,
        record_event=False,
        granted_scopes=["memory.admin"],
    )
    return await _page(
        request,
        project=project,
        from_harness=from_harness,
        to_harness=to_harness,
        briefing=render_briefing(payload, lang=get_lang()),
    )


@router.post("/download")
async def handoff_download(
    request: Request,
    project: str = Form(min_length=1, max_length=128),
    from_harness: str = Form(min_length=1, max_length=64),
    to_harness: str = Form(min_length=1, max_length=64),
):
    repo, _, _ = _deps(request)
    payload = await build_handoff(
        repo,
        project=project,
        from_harness=from_harness,
        to_harness=to_harness,
        top_k=20,
        granted_scopes=["memory.admin"],
    )
    return Response(
        render_briefing(payload, lang=get_lang()),
        media_type="text/plain",
        headers={**_PRIVATE, "Content-Disposition": 'attachment; filename="porter-handoff.txt"'},
    )
