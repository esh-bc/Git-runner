import logging
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
import httpx

# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
OWNER_ID  = 123456789  # Your Telegram user ID
PORT      = 8080       # Webserver port for uptime robot

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── In-Memory State ───────────────────────────────────────────────────────────
#
# allowed_users  : set of user_ids granted access by owner
#
# repos[user_id] : list of dicts, each dict is one repo entry:
#   {
#     "repo"         : "username/repo-name",
#     "token"        : "ghp_...",
#     "start_wf"     : "start.yml",
#     "stop_wf"      : "stop.yml",
#     "start_time"   : datetime | None,
#     "running"      : bool
#   }
#
# conv[user_id]  : conversation step tracking
#   {
#     "step"         : str,
#     "editing_index": int | None   (which repo is being edited/viewed)
#   }

allowed_users = set()
repos         = {}   # user_id -> list of repo dicts
conv          = {}   # user_id -> { step, editing_index }

# ── Helpers ───────────────────────────────────────────────────────────────────

def is_allowed(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in allowed_users

def get_repos(user_id: int) -> list:
    return repos.get(user_id, [])

def set_repos(user_id: int, repo_list: list):
    repos[user_id] = repo_list

def get_conv(user_id: int) -> dict:
    return conv.get(user_id, {"step": None, "editing_index": None, "temp": {}})

def set_conv(user_id: int, step: str, editing_index=None, temp=None):
    conv[user_id] = {
        "step": step,
        "editing_index": editing_index,
        "temp": temp or {}
    }

def elapsed(start_time: datetime) -> str:
    if not start_time:
        return "unknown"
    delta = datetime.now() - start_time
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s   = divmod(rem, 60)
    return f"{h}h {m}m {s}s"

def short_repo(repo_str: str) -> str:
    """Returns just the repo name part for display."""
    parts = repo_str.split("/")
    return parts[-1] if len(parts) > 1 else repo_str

# ── GitHub API ────────────────────────────────────────────────────────────────

async def validate_github(repo: str, token: str) -> bool:
    url     = f"https://api.github.com/repos/{repo}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept"       : "application/vnd.github+json"
    }
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=headers)
    return r.status_code == 200

async def trigger_workflow(repo: str, token: str, workflow_file: str) -> bool:
    """Trigger a workflow_dispatch on the given workflow file."""
    url     = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/dispatches"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept"       : "application/vnd.github+json"
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(url, headers=headers, json={"ref": "main"})
    return r.status_code == 204

async def cancel_workflow(repo: str, token: str, workflow_file: str) -> bool:
    """Find the latest in_progress or queued run of the workflow and cancel it."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept"       : "application/vnd.github+json"
    }
    async with httpx.AsyncClient() as client:
        # Step 1: Get runs for this workflow that are active
        list_url = (
            f"https://api.github.com/repos/{repo}/actions/workflows"
            f"/{workflow_file}/runs?status=in_progress&per_page=5"
        )
        r = await client.get(list_url, headers=headers)
        if r.status_code != 200:
            return False

        runs = r.json().get("workflow_runs", [])

        # Also check queued runs
        list_url_queued = (
            f"https://api.github.com/repos/{repo}/actions/workflows"
            f"/{workflow_file}/runs?status=queued&per_page=5"
        )
        r2 = await client.get(list_url_queued, headers=headers)
        if r2.status_code == 200:
            runs += r2.json().get("workflow_runs", [])

        if not runs:
            return False

        # Step 2: Cancel the most recent one
        run_id   = runs[0]["id"]
        cancel_url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/cancel"
        rc = await client.post(cancel_url, headers=headers)

        # 202 = accepted for cancellation
        return rc.status_code == 202

# ── Keyboards ─────────────────────────────────────────────────────────────────

def main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Shows all saved repos as buttons + Add Repo button."""
    user_repos = get_repos(user_id)
    buttons    = []
    for i, r in enumerate(user_repos):
        label = f"📦 {r['repo']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"view_{i}")])
    buttons.append([InlineKeyboardButton("➕ Add Repo", callback_data="add_repo")])
    return InlineKeyboardMarkup(buttons)

def repo_detail_keyboard(index: int, running: bool) -> InlineKeyboardMarkup:
    """Shows Start/Stop/Refresh/Remove/Back for a specific repo."""
    if running:
        top_row = [
            InlineKeyboardButton("⏹ Stop",    callback_data=f"stop_{index}"),
            InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_{index}")
        ]
    else:
        top_row = [
            InlineKeyboardButton("▶️ Start",   callback_data=f"start_{index}"),
            InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_{index}")
        ]
    bottom_row = [
        InlineKeyboardButton("🗑 Remove",  callback_data=f"remove_{index}"),
        InlineKeyboardButton("⬅️ Back",   callback_data="back_main")
    ]
    return InlineKeyboardMarkup([top_row, bottom_row])

def confirm_remove_keyboard(index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, remove", callback_data=f"confirm_remove_{index}"),
        InlineKeyboardButton("❌ Cancel",       callback_data=f"view_{index}")
    ]])

# ── Repo Detail Text ──────────────────────────────────────────────────────────

def repo_detail_text(r: dict) -> str:
    status = "🟢 Running" if r.get("running") else "🔴 Stopped"
    text   = (
        f"📦 *{r['repo']}*\n\n"
        f"Status   : {status}\n"
        f"Workflow : `{r['start_wf']}`\n"
    )
    if r.get("running") and r.get("start_time"):
        text += f"Running for : {elapsed(r['start_time'])}"
    return text

# ── Command Handlers ──────────────────────────────────────────────────────────

async def cmd_access(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Owner only: /access <user_id>"""
    if update.effective_user.id != OWNER_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /access <user_id>")
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    allowed_users.add(uid)
    await update.message.reply_text(f"✅ User {uid} has been granted access.")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point for all users."""
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text(
            f"Sorry {user.first_name}, I'm a premium bot 😎\n"
            f"You don't have access to use me."
        )
        return

    set_conv(user.id, step=None)
    user_repos = get_repos(user.id)

    if not user_repos:
        await update.message.reply_text(
            "👋 Welcome! You have no repos saved yet.\n\n"
            "Tap below to add your first one.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ Add Repo", callback_data="add_repo")
            ]])
        )
    else:
        await update.message.reply_text(
            "👋 Welcome back! Choose a repo to manage:",
            reply_markup=main_menu_keyboard(user.id)
        )

async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Owner only: /revoke <user_id>"""
    if update.effective_user.id != OWNER_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /revoke <user_id>")
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    allowed_users.discard(uid)
    await update.message.reply_text(f"✅ Access revoked for user {uid}.")

# ── Message Handler (Conversation Flow) ───────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user  = update.effective_user
    text  = update.message.text.strip()

    if not is_allowed(user.id):
        await update.message.reply_text(
            f"Sorry {user.first_name}, I'm a premium bot 😎\n"
            f"You don't have access to use me."
        )
        return

    c    = get_conv(user.id)
    step = c.get("step")
    temp = c.get("temp", {})

    # ── Step: awaiting repo URL ───────────────────────────────────────────────
    if step == "awaiting_repo":
        if "/" not in text:
            await update.message.reply_text(
                "❌ Please use the format `username/repo-name`",
                parse_mode="Markdown"
            )
            return
        temp["repo"] = text
        set_conv(user.id, step="awaiting_token", temp=temp)
        await update.message.reply_text(
            f"Got it! Repo: `{text}`\n\n"
            f"Now send your GitHub Personal Access Token\n"
            f"_(needs `workflow` permission)_",
            parse_mode="Markdown"
        )

    # ── Step: awaiting GitHub token ───────────────────────────────────────────
    elif step == "awaiting_token":
        await update.message.reply_text("⏳ Validating token...")
        repo  = temp.get("repo", "")
        valid = await validate_github(repo, text)
        if not valid:
            await update.message.reply_text(
                "❌ Could not connect. Check your repo URL and token then try again.\n\n"
                "Send repo URL again or /start to restart."
            )
            set_conv(user.id, step="awaiting_repo", temp={})
            await update.message.reply_text(
                "Send your repository URL:\nExample: `username/repo-name`",
                parse_mode="Markdown"
            )
            return
        temp["token"] = text
        set_conv(user.id, step="awaiting_start_wf", temp=temp)
        await update.message.reply_text(
            "✅ Token valid!\n\n"
            "What is your workflow filename?\n"
            "Example: `main.yml`",
            parse_mode="Markdown"
        )

    # ── Step: awaiting start workflow filename ────────────────────────────────
    elif step == "awaiting_start_wf":
        if not text.endswith(".yml") and not text.endswith(".yaml"):
            await update.message.reply_text(
                "❌ Must end in `.yml` or `.yaml`\nExample: `start.yml`",
                parse_mode="Markdown"
            )
            return
        temp["start_wf"] = text
        set_conv(user.id, step="awaiting_stop_wf", temp=temp)
        await update.message.reply_text(
            "Got it! Now what is your *stop* workflow filename?\n"
            "Example: `stop.yml`",
            parse_mode="Markdown"
        )

    # ── Step: awaiting workflow filename (used for both start & cancel) ───────
    elif step == "awaiting_start_wf":
        if not text.endswith(".yml") and not text.endswith(".yaml"):
            await update.message.reply_text(
                "❌ Must end in `.yml` or `.yaml`\nExample: `main.yml`",
                parse_mode="Markdown"
            )
            return
        temp["start_wf"] = text

        # Save the new repo entry
        new_entry = {
            "repo"      : temp["repo"],
            "token"     : temp["token"],
            "start_wf"  : temp["start_wf"],
            "start_time": None,
            "running"   : False
        }
        user_repos = get_repos(user.id)
        user_repos.append(new_entry)
        set_repos(user.id, user_repos)
        set_conv(user.id, step=None)

        await update.message.reply_text(
            f"✅ Repo saved!\n\n"
            f"📦 *{temp['repo']}*\n"
            f"Workflow: `{temp['start_wf']}`\n\n"
            f"Stop will automatically cancel the running workflow.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(user.id)
        )

    else:
        await update.message.reply_text(
            "Use /start to see your repos or manage access."
        )

# ── Callback Handler (Button Presses) ────────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user  = query.from_user
    data  = query.data
    await query.answer()

    if not is_allowed(user.id):
        await query.answer("You don't have access.", show_alert=True)
        return

    user_repos = get_repos(user.id)

    # ── Add Repo ──────────────────────────────────────────────────────────────
    if data == "add_repo":
        set_conv(user.id, step="awaiting_repo", temp={})
        await query.edit_message_text(
            "Let's add a new repo!\n\n"
            "Send your repository URL:\nExample: `username/repo-name`",
            parse_mode="Markdown"
        )

    # ── Back to Main Menu ─────────────────────────────────────────────────────
    elif data == "back_main":
        set_conv(user.id, step=None)
        if not user_repos:
            await query.edit_message_text(
                "You have no repos saved yet.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ Add Repo", callback_data="add_repo")
                ]])
            )
        else:
            await query.edit_message_text(
                "Choose a repo to manage:",
                reply_markup=main_menu_keyboard(user.id)
            )

    # ── View Repo Detail ──────────────────────────────────────────────────────
    elif data.startswith("view_"):
        index = int(data.split("_")[1])
        if index >= len(user_repos):
            await query.edit_message_text("Repo not found. Use /start to refresh.")
            return
        r = user_repos[index]
        await query.edit_message_text(
            repo_detail_text(r),
            parse_mode="Markdown",
            reply_markup=repo_detail_keyboard(index, r.get("running", False))
        )

    # ── Start Workflow ────────────────────────────────────────────────────────
    elif data.startswith("start_"):
        index = int(data.split("_")[1])
        if index >= len(user_repos):
            return
        r  = user_repos[index]
        ok = await trigger_workflow(r["repo"], r["token"], r["start_wf"])
        if ok:
            user_repos[index]["running"]    = True
            user_repos[index]["start_time"] = datetime.now()
            set_repos(user.id, user_repos)
            await query.edit_message_text(
                repo_detail_text(user_repos[index]),
                parse_mode="Markdown",
                reply_markup=repo_detail_keyboard(index, True)
            )
        else:
            await query.edit_message_text(
                f"❌ Failed to trigger `{r['start_wf']}`\n"
                f"Check your token has `workflow` permission and the file exists.",
                parse_mode="Markdown",
                reply_markup=repo_detail_keyboard(index, False)
            )

    # ── Stop Workflow (cancel running run) ───────────────────────────────────
    elif data.startswith("stop_"):
        index = int(data.split("_")[1])
        if index >= len(user_repos):
            return
        r  = user_repos[index]
        ok = await cancel_workflow(r["repo"], r["token"], r["start_wf"])
        if ok:
            user_repos[index]["running"]    = False
            user_repos[index]["start_time"] = None
            set_repos(user.id, user_repos)
            await query.edit_message_text(
                repo_detail_text(user_repos[index]),
                parse_mode="Markdown",
                reply_markup=repo_detail_keyboard(index, False)
            )
        else:
            await query.edit_message_text(
                "❌ Could not find a running workflow to cancel.\n"
                "It may have already finished or was never started.",
                parse_mode="Markdown",
                reply_markup=repo_detail_keyboard(index, False)
            )

    # ── Refresh ───────────────────────────────────────────────────────────────
    elif data.startswith("refresh_"):
        index = int(data.split("_")[1])
        if index >= len(user_repos):
            return
        r = user_repos[index]
        await query.edit_message_text(
            repo_detail_text(r),
            parse_mode="Markdown",
            reply_markup=repo_detail_keyboard(index, r.get("running", False))
        )

    # ── Remove (confirm prompt) ───────────────────────────────────────────────
    elif data.startswith("remove_") and not data.startswith("remove_confirm"):
        index = int(data.split("_")[1])
        if index >= len(user_repos):
            return
        r = user_repos[index]
        await query.edit_message_text(
            f"Are you sure you want to remove\n📦 *{r['repo']}*?",
            parse_mode="Markdown",
            reply_markup=confirm_remove_keyboard(index)
        )

    # ── Confirm Remove ────────────────────────────────────────────────────────
    elif data.startswith("confirm_remove_"):
        index = int(data.split("confirm_remove_")[1])
        if index >= len(user_repos):
            return
        removed = user_repos.pop(index)
        set_repos(user.id, user_repos)

        if not user_repos:
            await query.edit_message_text(
                f"🗑 Removed *{removed['repo']}*\n\nNo repos left. Add one below.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ Add Repo", callback_data="add_repo")
                ]])
            )
        else:
            await query.edit_message_text(
                f"🗑 Removed *{removed['repo']}*\n\nChoose a repo to manage:",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(user.id)
            )

# ── Ping Webserver ────────────────────────────────────────────────────────────

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ping":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"pong")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Silence default access logs

def run_webserver():
    server = HTTPServer(("0.0.0.0", PORT), PingHandler)
    logger.info(f"Ping server running on port {PORT}")
    server.serve_forever()

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Start ping webserver in background thread
    thread = threading.Thread(target=run_webserver, daemon=True)
    thread.start()

    
