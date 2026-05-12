"""SMTP email sender for fix-confirmation messages.

Emails are produced as multipart/alternative with both a plain-text and a
rich Hebrew HTML version. End recipients are non-developer customers,
so the HTML body is the primary view: it shows customer details, the
original ticket, what the bot did, files changed, and a big call-to-
action linking to the GitHub PR. The plain-text version contains the
same information without markup so that it survives any client that
strips HTML.
"""
from __future__ import annotations

import html
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from auto_bug_fixer.config import Settings
from auto_bug_fixer.logging_setup import get_logger
from auto_bug_fixer.models import Bug, FixOutcome, PullRequest

log = get_logger(__name__)

SMTP_TIMEOUT_SECONDS = 30
PRODUCT_NAME = "auto-bug-fixer"
SUBJECT_BRAND = f"[{PRODUCT_NAME}]"


def _resolve_israel_tz() -> tuple[ZoneInfo | timezone, str]:
    """Best-effort Asia/Jerusalem; falls back to UTC if tzdata is missing.

    Windows ships no IANA tz database by default. We do not want to add
    the ``tzdata`` package just for a single timestamp string in an
    email, and the production runtime (Linux CI) has the database
    built in.
    """
    try:
        return ZoneInfo("Asia/Jerusalem"), "%d/%m/%Y %H:%M %Z"
    except ZoneInfoNotFoundError:
        return timezone.utc, "%d/%m/%Y %H:%M UTC"


ISRAEL_TZ, _NOW_FMT = _resolve_israel_tz()


class EmailDeliveryError(RuntimeError):
    """Raised when SMTP delivery fails."""


class EmailNotifier:
    """Sends success / failure confirmations via SMTP."""

    def __init__(self, settings: Settings) -> None:
        """Bind a notifier to its SMTP credentials."""
        self._settings = settings

    def notify_success(
        self,
        bug: Bug,
        outcome: FixOutcome,
        pr: PullRequest,
    ) -> None:
        """Send a rich Hebrew success email about ``pr`` to the bug reporter."""
        if not self._settings.email_enabled:
            log.info("skip_email_disabled", bug_id=bug.id)
            return
        if not bug.reporter_email:
            log.info("skip_email_no_reporter", bug_id=bug.id)
            return
        subject = (
            f"{SUBJECT_BRAND} תיקון מוכן לבדיקה — באג {bug.id}: "
            f"{_short(bug.title, 80)}"
        )
        text_body = _render_success_text(bug, outcome, pr)
        html_body = _render_success_html(bug, outcome, pr)
        self._send(
            to=bug.reporter_email,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )

    def notify_failure(self, bug: Bug, outcome: FixOutcome) -> None:
        """Send a rich Hebrew failure email so a human knows to take over."""
        if not self._settings.email_enabled:
            log.info("skip_email_disabled", bug_id=bug.id)
            return
        if not bug.reporter_email:
            log.info("skip_email_no_reporter", bug_id=bug.id)
            return
        subject = (
            f"{SUBJECT_BRAND} Could not auto-fix bug {bug.id} — נדרש טיפול ידני"
        )
        text_body = _render_failure_text(bug, outcome)
        html_body = _render_failure_html(bug, outcome)
        self._send(
            to=bug.reporter_email,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )

    def _send(
        self,
        *,
        to: str,
        subject: str,
        text_body: str,
        html_body: str,
    ) -> None:
        s = self._settings
        message = EmailMessage()
        message["From"] = s.notify_from
        message["To"] = to
        if s.notify_cc:
            message["Cc"] = s.notify_cc
        message["Subject"] = subject
        message.set_content(text_body)
        message.add_alternative(html_body, subtype="html")

        recipients = [to] + [
            addr.strip() for addr in s.notify_cc.split(",") if addr.strip()
        ]
        try:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
                smtp.send_message(message, to_addrs=recipients)
        except (smtplib.SMTPException, OSError) as exc:
            raise EmailDeliveryError(f"SMTP send failed: {exc}") from exc

        log.info("email_sent", to=to, subject=subject)


# ---------------------------------------------------------------------------
# Plain-text rendering
# ---------------------------------------------------------------------------


def _render_success_text(bug: Bug, outcome: FixOutcome, pr: PullRequest) -> str:
    """Plain-text fallback. Mirrors the HTML version section by section."""
    files = "\n".join(f"  - {p}" for p in outcome.changed_files) or "  (אין)"
    vercel = _build_vercel_preview_url(bug, pr)
    vercel_line = f"Preview  : {vercel}\n" if vercel else ""
    return (
        f"{PRODUCT_NAME} — תיקון אוטומטי מוכן לבדיקה\n"
        f"{'=' * 60}\n\n"
        "שלום,\n"
        "בוט התיקון האוטומטי סיים לטפל בתקלה ופתח Pull Request.\n"
        "בבקשה עברי על השינוי, ואם הוא נראה לך — מזגי את ה-PR.\n\n"
        f"  📁 {len(outcome.changed_files)} קבצים שהשתנו  |  "
        f"🔀 PR #{pr.number}\n\n"
        "--- פרטי התקלה ---\n"
        f"מזהה תקלה   : {bug.id}\n"
        f"פרויקט      : {_or_dash(bug.project_name)}\n"
        f"לקוח/ה      : {_or_dash(bug.customer_name)}\n"
        f"מייל הלקוח/ה: {_or_dash(bug.reporter_email)}\n"
        f"סוג טיקט    : {bug.ticket_type}\n"
        f"זמן שליחה   : {_now_str()}\n\n"
        "--- תיאור התקלה (מהלקוח) ---\n"
        f"{bug.description}\n\n"
        "--- מה הבוט עשה (Claude AI) ---\n"
        f"{outcome.summary}\n\n"
        f"--- קבצים שהשתנו ({len(outcome.changed_files)}) ---\n"
        f"{files}\n\n"
        "--- ה-Pull Request ---\n"
        f"כותרת   : {pr.title}\n"
        f"מספר    : #{pr.number}\n"
        f"סניף חדש: {pr.branch}\n"
        f"סניף בסיס: {bug.base_branch}\n"
        f"ריפו    : {bug.repo_url}\n"
        f"קישור   : {pr.url}\n"
        f"{vercel_line}\n"
        "--- ציר זמן ---\n"
        "  1. 📩 הלקוח/ה דיווח/ה על באג\n"
        "  2. 🤖 הבוט קיבל וניתח את הדיווח\n"
        "  3. 🔍 הבוט חקר את הקוד ומצא את הבעיה\n"
        f"  4. 🔧 הבוט שינה {len(outcome.changed_files)} קבצים\n"
        f"  5. 📤 נפתח PR #{pr.number}\n"
        "  6. 📧 נשלח המייל הזה\n\n"
        "--- הצעדים הבאים ---\n"
        "  1. פתחי את ה-PR בקישור למעלה.\n"
        "  2. עברי על השינויים בלשונית \"Files changed\".\n"
        "  3. בדקי את התצוגה המקדימה של Vercel (אם קיימת).\n"
        "  4. אם הכל בסדר — לחצי \"Merge pull request\".\n"
        "  5. אם משהו לא נראה — סגרי את ה-PR או השאירי הערה.\n\n"
        "בהצלחה!\n"
        f"-- {PRODUCT_NAME}\n"
    )


def _render_failure_text(bug: Bug, outcome: FixOutcome) -> str:
    return (
        f"{PRODUCT_NAME} — לא הצלחנו לתקן את הבאג אוטומטית\n"
        f"{'=' * 60}\n\n"
        "שלום,\n"
        "בוט התיקון לא הצליח להפיק תיקון תקין לתקלה הבאה.\n"
        "נדרש טיפול ידני של מפתח/ת.\n\n"
        "--- פרטי התקלה ---\n"
        f"מזהה תקלה   : {bug.id}\n"
        f"פרויקט      : {_or_dash(bug.project_name)}\n"
        f"לקוח/ה      : {_or_dash(bug.customer_name)}\n"
        f"מייל הלקוח/ה: {_or_dash(bug.reporter_email)}\n"
        f"זמן ניסיון  : {_now_str()}\n\n"
        "--- תיאור התקלה (מהלקוח) ---\n"
        f"{bug.description}\n\n"
        "--- מה השתבש ---\n"
        f"{outcome.error or 'לא ידוע'}\n\n"
        "--- הערות הבוט ---\n"
        f"{outcome.summary}\n\n"
        f"-- {PRODUCT_NAME}\n"
    )


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def _render_success_html(bug: Bug, outcome: FixOutcome, pr: PullRequest) -> str:
    files_html = (
        "".join(
            f'<li style="margin-bottom:4px;">'
            f'<a href="{html.escape(pr.url)}/files" '
            f'style="color:#1565c0;text-decoration:none;">'
            f'<code style="font-family:Consolas,Monaco,monospace;'
            f'background:#f4f4f4;padding:2px 6px;border-radius:3px;">'
            f'{html.escape(p)}</code></a></li>'
            for p in outcome.changed_files
        )
        or "<li><em>אין</em></li>"
    )

    # Build Vercel preview URL from the PR branch
    vercel_preview = _build_vercel_preview_url(bug, pr)

    # Stats bar
    stats_items = [
        f'<span style="margin-left:16px;">📁 {len(outcome.changed_files)} קבצים</span>',
        f'<span style="margin-left:16px;">🔀 PR #{pr.number}</span>',
    ]
    if vercel_preview:
        stats_items.append(
            f'<span style="margin-left:16px;">🌐 '
            f'<a href="{html.escape(vercel_preview, quote=True)}" '
            f'style="color:#1565c0;text-decoration:none;">Preview</a></span>'
        )
    stats_bar = (
        '<div style="padding:12px 24px;background:#f0f7ef;'
        'border-bottom:1px solid #e0e0e0;text-align:center;'
        'font-size:14px;color:#333;">'
        + "".join(stats_items)
        + '</div>'
    )

    sections = [
        _banner_html(
            emoji="✅",
            heading="התיקון מוכן לבדיקה!",
            subheading=(
                f"באג <code>{html.escape(bug.id)}</code> טופל אוטומטית "
                "ונפתח Pull Request לבדיקתך."
            ),
            accent="#2e7d32",
        ),
        stats_bar,
        _details_card_html(
            "פרטי התקלה",
            rows=[
                ("מזהה תקלה", html.escape(bug.id), True),
                ("פרויקט", _or_dash_html(bug.project_name), False),
                ("לקוח/ה", _or_dash_html(bug.customer_name), False),
                ("מייל הלקוח/ה", _or_dash_html(bug.reporter_email), False),
                ("סוג טיקט", html.escape(bug.ticket_type), False),
                ("זמן שליחת המייל", html.escape(_now_str()), False),
            ],
        ),
        _quote_card_html(
            'תיאור התקלה (כפי שדווחה ע"י הלקוח/ה)',
            bug.description,
        ),
        _ai_summary_card_html(outcome.summary),
        _list_card_html(
            f"קבצים שהשתנו ({len(outcome.changed_files)})",
            files_html,
        ),
        _details_card_html(
            "פרטי ה-Pull Request",
            rows=[
                ("כותרת", html.escape(pr.title), False),
                ("מספר", f"#{pr.number}", True),
                ("סניף חדש", html.escape(pr.branch), True),
                ("סניף בסיס", html.escape(bug.base_branch), True),
                ("ריפו", _link_html(bug.repo_url), False),
            ],
        ),
        _cta_html("עברי לבדיקת ה-PR ב-GitHub", pr.url, "#2e7d32"),
    ]

    if vercel_preview:
        sections.append(
            _cta_html("צפי בתצוגה מקדימה (Vercel Preview)", vercel_preview, "#0070f3")
        )

    sections.append(
        _timeline_html([
            ("📩", "הלקוח/ה דיווח/ה על באג"),
            ("🤖", "הבוט קיבל את הדיווח וניתח אותו"),
            ("🔍", "הבוט חקר את הקוד ומצא את הבעיה"),
            ("🔧", f"הבוט שינה {len(outcome.changed_files)} קבצים"),
            ("📤", f"נפתח PR #{pr.number} לבדיקתך"),
            ("📧", "נשלח המייל הזה — ממתינים לבדיקה!"),
        ])
    )

    sections.append(
        _next_steps_html([
            'פתחי את ה-PR בקישור למעלה.',
            'עברי על השינויים בלשונית "Files changed".',
            'בדקי את התצוגה המקדימה של Vercel (אם קיימת).',
            'אם הכל בסדר — לחצי "Merge pull request".',
            'אם משהו לא נראה — סגרי את ה-PR או השאירי הערה.',
        ])
    )

    return _shell_html(
        title=f"{PRODUCT_NAME} — תיקון מוכן לבדיקה",
        accent="#2e7d32",
        sections=sections,
    )


def _render_failure_html(bug: Bug, outcome: FixOutcome) -> str:
    sections = [
        _banner_html(
            emoji="⚠️",
            heading="לא הצלחנו לתקן את הבאג אוטומטית",
            subheading=(
                f"באג <code>{html.escape(bug.id)}</code> דורש טיפול ידני "
                "של מפתח/ת."
            ),
            accent="#c62828",
        ),
        # Urgency bar
        '<div style="padding:12px 24px;background:#fef2f2;'
        'border-bottom:1px solid #fecaca;text-align:center;'
        'font-size:14px;color:#991b1b;">'
        '⏰ נדרש טיפול ידני — הבוט לא הצליח לפתור את הבעיה'
        '</div>',
        _details_card_html(
            "פרטי התקלה",
            rows=[
                ("מזהה תקלה", html.escape(bug.id), True),
                ("פרויקט", _or_dash_html(bug.project_name), False),
                ("לקוח/ה", _or_dash_html(bug.customer_name), False),
                ("מייל הלקוח/ה", _or_dash_html(bug.reporter_email), False),
                ("זמן ניסיון", html.escape(_now_str()), False),
            ],
        ),
        _quote_card_html(
            'תיאור התקלה (כפי שדווחה ע"י הלקוח/ה)',
            bug.description,
        ),
        _card_html(
            title="מה השתבש",
            inner=(
                '<div style="background:#fef2f2;border-right:4px solid #dc2626;'
                'padding:12px 16px;font-size:14px;color:#991b1b;line-height:1.55;'
                'border-radius:4px;white-space:normal;word-break:break-word;">'
                f'{html.escape(outcome.error or "לא ידוע").replace(chr(10), "<br>")}'
                '</div>'
            ),
        ),
        _ai_summary_card_html(outcome.summary),
        _timeline_html([
            ("📩", "הלקוח/ה דיווח/ה על באג"),
            ("🤖", "הבוט קיבל את הדיווח וניתח אותו"),
            ("🔍", "הבוט ניסה לחקור את הקוד"),
            ("❌", "הבוט לא הצליח לפתור את הבעיה"),
            ("📧", "נשלח מייל הודעה — נדרש טיפול ידני"),
        ]),
    ]
    return _shell_html(
        title=f"{PRODUCT_NAME} — תיקון אוטומטי נכשל",
        accent="#c62828",
        sections=sections,
    )


# ---------------------------------------------------------------------------
# HTML primitives
# ---------------------------------------------------------------------------

_BASE_FONT = (
    "font-family:'Segoe UI',Tahoma,Arial,'Helvetica Neue',Helvetica,sans-serif;"
)


def _shell_html(*, title: str, accent: str, sections: list[str]) -> str:
    body = "\n".join(sections)
    return (
        '<!doctype html>'
        '<html dir="rtl" lang="he">'
        '<head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{html.escape(title)}</title>'
        '</head>'
        f'<body style="margin:0;padding:0;background:#f0f2f5;{_BASE_FONT}'
        'color:#1f2933;direction:rtl;text-align:right;'
        '-webkit-font-smoothing:antialiased;">'
        '<div style="max-width:680px;margin:0 auto;padding:24px;">'
        # Logo header
        '<div style="text-align:center;padding:12px 0 20px 0;">'
        '<div style="display:inline-block;font-size:18px;font-weight:700;'
        f'color:{accent};letter-spacing:-0.5px;">'
        f'🤖 {html.escape(PRODUCT_NAME)}'
        '</div>'
        '</div>'
        f'<div style="background:#ffffff;border-radius:12px;'
        f'border-top:6px solid {accent};'
        'box-shadow:0 2px 8px rgba(0,0,0,0.06),0 0 1px rgba(0,0,0,0.1);'
        'overflow:hidden;">'
        f'{body}'
        '<div style="padding:18px 24px;'
        'background:linear-gradient(135deg,#fafafa 0%,#f5f5f5 100%);'
        'border-top:1px solid #eee;font-size:12px;color:#888;'
        'text-align:center;">'
        f'נשלח אוטומטית ע"י <strong>{html.escape(PRODUCT_NAME)}</strong> '
        f'| {html.escape(_now_str())}<br>'
        '<span style="color:#aaa;">Powered by Claude AI</span>'
        '</div>'
        '</div>'
        '</div>'
        '</body></html>'
    )


def _banner_html(*, emoji: str, heading: str, subheading: str, accent: str) -> str:
    return (
        '<div style="padding:28px 24px 18px 24px;text-align:center;">'
        f'<div style="font-size:42px;line-height:1;margin-bottom:8px;">{emoji}</div>'
        f'<div style="font-size:22px;font-weight:600;color:{accent};'
        f'margin-bottom:6px;">{html.escape(heading)}</div>'
        f'<div style="font-size:14px;color:#555;">{subheading}</div>'
        '</div>'
    )


def _details_card_html(title: str, *, rows: list[tuple[str, str, bool]]) -> str:
    """Render a label/value table.

    Each row is ``(label, value_html, mono)`` — when ``mono`` is True the
    value is rendered in a monospace box (good for IDs / branches / paths).
    The ``value_html`` field is treated as raw HTML, callers are
    responsible for escaping it.
    """
    rows_html = "".join(
        '<tr>'
        '<td style="padding:6px 0;font-size:13px;color:#666;'
        'width:130px;vertical-align:top;">'
        f'{html.escape(label)}</td>'
        '<td style="padding:6px 0;font-size:14px;color:#1f2933;'
        'vertical-align:top;">'
        f'{_mono_wrap(value_html) if mono else value_html}'
        '</td>'
        '</tr>'
        for label, value_html, mono in rows
    )
    return _card_html(
        title=title,
        inner=f'<table style="width:100%;border-collapse:collapse;">{rows_html}</table>',
    )


def _quote_card_html(title: str, raw_text: str) -> str:
    body = html.escape(raw_text or "(ריק)").replace("\n", "<br>")
    return _card_html(
        title=title,
        inner=(
            '<div style="background:#f8f9fb;border-right:4px solid #cfd4dc;'
            'padding:12px 16px;font-size:14px;color:#333;line-height:1.55;'
            'border-radius:4px;white-space:normal;word-break:break-word;">'
            f'{body}'
            '</div>'
        ),
    )


def _list_card_html(title: str, list_items_html: str) -> str:
    return _card_html(
        title=title,
        inner=(
            '<ul style="margin:0;padding:0 18px 0 0;font-size:14px;'
            f'color:#1f2933;line-height:1.8;">{list_items_html}</ul>'
        ),
    )


def _card_html(*, title: str, inner: str) -> str:
    return (
        '<div style="padding:0 24px 18px 24px;">'
        '<div style="font-size:13px;font-weight:600;color:#999;'
        'text-transform:uppercase;letter-spacing:0.5px;'
        f'margin-bottom:8px;">{html.escape(title)}</div>'
        f'{inner}'
        '</div>'
    )


def _cta_html(label: str, url: str, accent: str) -> str:
    safe_url = html.escape(url, quote=True)
    return (
        '<div style="padding:8px 24px 24px 24px;text-align:center;">'
        f'<a href="{safe_url}" style="display:inline-block;'
        f'background:{accent};color:#ffffff;text-decoration:none;'
        'padding:14px 28px;border-radius:6px;font-size:15px;font-weight:600;'
        f'box-shadow:0 1px 2px rgba(0,0,0,0.12);">{html.escape(label)} ↗</a>'
        '<div style="margin-top:10px;font-size:12px;color:#888;'
        'word-break:break-all;direction:ltr;text-align:center;">'
        f'{safe_url}'
        '</div>'
        '</div>'
    )


def _build_vercel_preview_url(bug: Bug, pr: PullRequest) -> str | None:
    """Return None — Vercel preview URLs use unpredictable hashes.

    The Vercel bot posts the real preview URL as a PR comment, so we
    direct users to the PR instead of guessing a broken URL.
    """
    return None


def _ai_summary_card_html(summary: str) -> str:
    """Render the AI summary with a distinctive style."""
    body = html.escape(summary or "(אין סיכום)").replace("\n", "<br>")
    return _card_html(
        title="מה הבוט עשה (סיכום AI)",
        inner=(
            '<div style="background:linear-gradient(135deg,#f3e7ff 0%,#e8f4fd 100%);'
            'border-right:4px solid #7c3aed;'
            'padding:14px 18px;font-size:14px;color:#1f2937;line-height:1.6;'
            'border-radius:6px;white-space:normal;word-break:break-word;">'
            '<div style="font-size:12px;color:#6b21a8;font-weight:600;'
            'margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px;">'
            '🤖 Claude AI Analysis</div>'
            f'{body}'
            '</div>'
        ),
    )


def _timeline_html(steps: list[tuple[str, str]]) -> str:
    """Render a visual timeline of the fix process."""
    items = []
    for i, (icon, text) in enumerate(steps):
        is_last = i == len(steps) - 1
        connector = (
            "" if is_last else
            '<div style="position:absolute;right:11px;top:28px;bottom:-8px;'
            'width:2px;background:#e0e0e0;"></div>'
        )
        items.append(
            f'<div style="position:relative;padding-right:36px;'
            f'padding-bottom:{0 if is_last else 12}px;min-height:28px;">'
            f'<div style="position:absolute;right:0;top:0;width:24px;height:24px;'
            f'border-radius:50%;background:#f0f7ef;border:2px solid #2e7d32;'
            f'text-align:center;font-size:12px;line-height:24px;">{icon}</div>'
            f'{connector}'
            f'<div style="font-size:13px;color:#333;padding-top:3px;">'
            f'{html.escape(text)}</div>'
            f'</div>'
        )
    return _card_html(
        title="ציר זמן",
        inner='<div style="position:relative;">' + "".join(items) + '</div>',
    )


def _next_steps_html(steps: list[str]) -> str:
    items = "".join(
        f'<li style="margin-bottom:6px;">{html.escape(s)}</li>'
        for s in steps
    )
    return _card_html(
        title="מה הצעדים הבאים?",
        inner=(
            '<ol style="margin:0;padding:0 22px 0 0;font-size:14px;'
            f'color:#1f2933;line-height:1.7;">{items}</ol>'
        ),
    )


def _mono_wrap(value_html: str) -> str:
    return (
        '<code style="font-family:Consolas,Monaco,monospace;background:#f4f4f4;'
        f'padding:2px 6px;border-radius:3px;direction:ltr;">{value_html}</code>'
    )


def _link_html(url: str) -> str:
    safe_url = html.escape(url, quote=True)
    return (
        f'<a href="{safe_url}" style="color:#1565c0;text-decoration:none;'
        f'word-break:break-all;direction:ltr;">{safe_url}</a>'
    )


# ---------------------------------------------------------------------------
# Small string helpers
# ---------------------------------------------------------------------------


def _or_dash(value: str | None) -> str:
    return value if value else "—"


def _or_dash_html(value: str | None) -> str:
    return html.escape(value) if value else "—"


def _short(value: str, limit: int) -> str:
    value = value or ""
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _now_str() -> str:
    """Local Israel time, e.g. 12/05/2026 16:42 IDT (UTC fallback on Windows)."""
    now = datetime.now(timezone.utc).astimezone(ISRAEL_TZ)
    return now.strftime(_NOW_FMT)
