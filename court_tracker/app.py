"""Flask application factory for Трекер судебных дел v2."""
import logging
import sqlite3
import threading
from datetime import datetime, timedelta

from flask import Flask, g, jsonify, redirect, render_template, request, flash, url_for

from court_tracker.config import DB_PATH, FLASK_HOST, FLASK_PORT, FLASK_DEBUG
from court_tracker.db.schema import init_db, run_migrations
from court_tracker.db import queries

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sync state (shared across threads)
# ---------------------------------------------------------------------------
_sync_lock = threading.Lock()
_sync_state = {"is_running": False, "last_sync": None}


def _background_sync(app: Flask) -> None:
    with app.app_context():
        _sync_state["is_running"] = True
        try:
            conn = _get_db()
            cases = queries.get_all_cases(conn, {"source": "kad"})
            from court_tracker.scraper.kad_scraper import KADScraper
            for case in cases:
                if not case.get("kad_url"):
                    continue
                try:
                    with KADScraper() as scraper:
                        details = scraper.get_case_details(case["kad_url"])
                    if details:
                        details["case_number"] = case["case_number"]
                        cid = queries.upsert_case(conn, details)
                        queries.save_participants(conn, cid, details.get("participants", []))
                        queries.save_events(conn, cid, details.get("events", []))
                        queries.log_sync(conn, cid, True, "startup sync")
                except Exception as exc:
                    queries.log_sync(conn, case["id"], False, str(exc))
            _sync_state["last_sync"] = datetime.utcnow().isoformat()
        except Exception as exc:
            logger.error("background sync error: %s", exc)
        finally:
            _sync_state["is_running"] = False


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        run_migrations(conn)
        g.db = conn
    return g.db


def _close_db(exc=None) -> None:
    conn = g.pop("db", None)
    if conn:
        conn.close()


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = "court-tracker-dev-secret"

    app.teardown_appcontext(_close_db)

    # Startup sync
    @app.before_request
    def _ensure_db():
        _get_db()

    with app.app_context():
        conn = _get_db()
        if queries.get_setting(conn, "sync_on_startup") == "1":
            t = threading.Thread(target=_background_sync, args=(app,), daemon=True)
            t.start()

    # ------------------------------------------------------------------
    # Template helpers
    # ------------------------------------------------------------------

    @app.template_filter("urgency_class")
    def urgency_class(deadline_date_str: str) -> str:
        try:
            d = datetime.strptime(deadline_date_str[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return "urgency-safe"
        days = (d - datetime.utcnow().date()).days
        if days <= 1:
            return "urgency-crit"
        if days <= 3:
            return "urgency-soon"
        if days <= 7:
            return "urgency-warn"
        return "urgency-safe"

    @app.template_filter("days_until")
    def days_until(date_str: str) -> int:
        try:
            d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
            return (d - datetime.utcnow().date()).days
        except (ValueError, TypeError):
            return 999

    @app.context_processor
    def inject_globals():
        conn = _get_db()
        unread = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE is_read=0"
        ).fetchone()[0]
        return {"unread_count": unread, "now": datetime.utcnow()}

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------

    @app.route("/")
    def index():
        conn = _get_db()
        all_cases = queries.get_all_cases(conn)
        this_month = datetime.utcnow().strftime("%Y-%m")
        active = [c for c in all_cases if c["status"] not in ("Завершено", "Прекращено", None)]
        new_this_month = [c for c in all_cases if (c.get("created_at") or "")[:7] == this_month]
        kad_count = sum(1 for c in all_cases if c["source"] == "kad")
        soy_count = sum(1 for c in all_cases if c["source"] == "soy")

        upcoming_hearings = queries.get_upcoming_hearings(conn, days=14)
        approaching_deadlines = queries.get_approaching_deadlines(conn, days=14)

        recent_logs = conn.execute(
            """SELECT sl.*, c.case_number FROM sync_log sl
               LEFT JOIN cases c ON c.id = sl.case_id
               ORDER BY sl.synced_at DESC LIMIT 5"""
        ).fetchall()

        kanban_counts = dict(
            conn.execute(
                "SELECT stage, COUNT(*) FROM kanban_stage GROUP BY stage"
            ).fetchall()
        )

        return render_template(
            "index.html",
            total_active=len(active),
            new_this_month=len(new_this_month),
            kad_count=kad_count,
            soy_count=soy_count,
            upcoming_hearings=upcoming_hearings,
            approaching_deadlines=approaching_deadlines,
            recent_logs=recent_logs,
            kanban_counts=kanban_counts,
        )

    # ------------------------------------------------------------------
    # Cases
    # ------------------------------------------------------------------

    @app.route("/cases")
    def cases_list():
        conn = _get_db()
        filters = {
            k: request.args.get(k)
            for k in ("source", "status", "client_id")
            if request.args.get(k)
        }
        case_list = queries.get_all_cases(conn, filters or None)
        clients = conn.execute("SELECT id, name FROM clients ORDER BY name").fetchall()
        statuses = conn.execute(
            "SELECT DISTINCT status FROM cases WHERE status IS NOT NULL ORDER BY status"
        ).fetchall()
        return render_template(
            "cases.html",
            cases=case_list,
            clients=clients,
            statuses=statuses,
            filters=filters,
        )

    @app.route("/cases/<int:case_id>")
    def case_detail(case_id: int):
        conn = _get_db()
        case = queries.get_case_full(conn, case_id)
        if not case:
            flash("Дело не найдено.", "danger")
            return redirect(url_for("cases_list"))
        clients = conn.execute("SELECT id, name FROM clients ORDER BY name").fetchall()
        return render_template("case_detail.html", case=case, clients=clients)

    @app.route("/cases/add", methods=["GET", "POST"])
    def case_add():
        conn = _get_db()
        clients = conn.execute("SELECT id, name FROM clients ORDER BY name").fetchall()
        if request.method == "POST":
            tab = request.form.get("tab", "kad")
            if tab == "kad":
                case_number = request.form.get("case_number", "").strip()
                if not case_number:
                    flash("Введите номер дела.", "danger")
                    return render_template("add_case.html", clients=clients, active_tab="kad")
                from court_tracker.scraper.utils import normalize_case_number
                from court_tracker.scraper.kad_scraper import KADScraper
                case_number = normalize_case_number(case_number)
                try:
                    with KADScraper() as scraper:
                        result = scraper.search_by_case_number(case_number)
                        details = None
                        if result and result.get("kad_url"):
                            details = scraper.get_case_details(result["kad_url"])
                except Exception as exc:
                    flash(f"Ошибка парсинга: {exc}", "danger")
                    return render_template("add_case.html", clients=clients, active_tab="kad")
                if not result:
                    flash(f"Дело {case_number} не найдено на КАД.", "warning")
                    return render_template("add_case.html", clients=clients, active_tab="kad")
                data = details if details else result
                data.setdefault("case_number", case_number)
                data.setdefault("source", "kad")
                case_id = queries.upsert_case(conn, data)
                if details:
                    queries.save_participants(conn, case_id, details.get("participants", []))
                    queries.save_events(conn, case_id, details.get("events", []))
                queries.log_sync(conn, case_id, True, "manual add-case")
                flash(f"Дело {case_number} добавлено.", "success")
                return redirect(url_for("case_detail", case_id=case_id))
            else:
                # SOY manual entry
                f = request.form
                data = {
                    "case_number": f.get("case_number", "").strip(),
                    "source": "soy",
                    "court": f.get("court"),
                    "judge": f.get("judge"),
                    "status": f.get("status"),
                    "case_type": f.get("case_type"),
                    "start_date": f.get("start_date"),
                    "soy_url_first": f.get("soy_url_first"),
                    "soy_url_appeal": f.get("soy_url_appeal"),
                    "soy_url_cassation": f.get("soy_url_cassation"),
                    "custom_label": f.get("custom_label"),
                    "client_id": f.get("client_id") or None,
                }
                if not data["case_number"]:
                    flash("Введите номер дела.", "danger")
                    return render_template("add_case.html", clients=clients, active_tab="soy")
                case_id = queries.upsert_case(conn, data)
                # Add plaintiff / respondent as participants
                for role, name_key in (("Истец", "plaintiff_name"), ("Ответчик", "respondent_name")):
                    name = f.get(name_key, "").strip()
                    if name:
                        conn.execute(
                            "INSERT INTO participants(case_id, role, name) VALUES (?,?,?)",
                            (case_id, role, name),
                        )
                conn.commit()
                flash(f"Дело {data['case_number']} добавлено (СОЮ).", "success")
                return redirect(url_for("case_detail", case_id=case_id))
        return render_template("add_case.html", clients=clients, active_tab="kad")

    @app.route("/cases/<int:case_id>/delete", methods=["POST"])
    def case_delete(case_id: int):
        conn = _get_db()
        case = queries.get_case_full(conn, case_id)
        if case:
            conn.execute("DELETE FROM cases WHERE id = ?", (case_id,))
            conn.commit()
            flash(f"Дело {case['case_number']} удалено.", "success")
        return redirect(url_for("index"))

    @app.route("/cases/<int:case_id>/sync", methods=["POST"])
    def case_sync(case_id: int):
        conn = _get_db()
        case = queries.get_case_full(conn, case_id)
        if not case or not case.get("kad_url"):
            flash("Нет URL для синхронизации.", "warning")
            return redirect(url_for("case_detail", case_id=case_id))
        try:
            from court_tracker.scraper.kad_scraper import KADScraper
            with KADScraper() as scraper:
                details = scraper.get_case_details(case["kad_url"])
            if details:
                details["case_number"] = case["case_number"]
                queries.upsert_case(conn, details)
                queries.save_participants(conn, case_id, details.get("participants", []))
                queries.save_events(conn, case_id, details.get("events", []))
                queries.log_sync(conn, case_id, True, "manual sync")
                flash("Синхронизация выполнена.", "success")
            else:
                queries.log_sync(conn, case_id, False, "scraper returned None")
                flash("Ошибка синхронизации.", "danger")
        except Exception as exc:
            queries.log_sync(conn, case_id, False, str(exc))
            flash(f"Ошибка: {exc}", "danger")
        return redirect(url_for("case_detail", case_id=case_id))

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        conn = _get_db()
        if request.method == "POST":
            queries.set_setting(conn, "sync_interval_hours", request.form.get("sync_interval_hours", "2"))
            queries.set_setting(conn, "sync_on_startup", "1" if request.form.get("sync_on_startup") else "0")
            flash("Настройки сохранены.", "success")
            return redirect(url_for("settings"))
        current = {
            "sync_interval_hours": queries.get_setting(conn, "sync_interval_hours", "2"),
            "sync_on_startup": queries.get_setting(conn, "sync_on_startup", "1"),
            "app_version": queries.get_setting(conn, "app_version", "2.0"),
        }
        return render_template("settings.html", settings=current)

    # ------------------------------------------------------------------
    # JSON API
    # ------------------------------------------------------------------

    @app.route("/api/autosave", methods=["POST"])
    def api_autosave():
        data = request.get_json(force=True, silent=True) or {}
        table = data.get("table", "")
        rec_id = data.get("id")
        field = data.get("field", "")
        value = data.get("value")
        if not table or rec_id is None or not field:
            return jsonify({"success": False, "error": "missing fields"}), 400
        from court_tracker.services.autosave import autosave_field
        ok = autosave_field(table, int(rec_id), field, value)
        if ok:
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "field not allowed or record not found"}), 400

    @app.route("/api/cases")
    def api_cases():
        conn = _get_db()
        return jsonify(queries.get_all_cases(conn))

    @app.route("/api/cases/<int:case_id>")
    def api_case(case_id: int):
        conn = _get_db()
        case = queries.get_case_full(conn, case_id)
        if not case:
            return jsonify({"error": "not found"}), 404
        return jsonify(dict(case))

    @app.route("/api/upcoming")
    def api_upcoming():
        conn = _get_db()
        return jsonify(queries.get_upcoming_hearings(conn, days=30))

    @app.route("/api/deadlines")
    def api_deadlines():
        conn = _get_db()
        return jsonify(queries.get_approaching_deadlines(conn, days=14))

    @app.route("/api/sync/<int:case_id>", methods=["POST"])
    def api_sync(case_id: int):
        conn = _get_db()
        case = queries.get_case_full(conn, case_id)
        if not case or not case.get("kad_url"):
            return jsonify({"success": False, "message": "no KAD URL"}), 400
        try:
            from court_tracker.scraper.kad_scraper import KADScraper
            with KADScraper() as scraper:
                details = scraper.get_case_details(case["kad_url"])
            if details:
                details["case_number"] = case["case_number"]
                queries.upsert_case(conn, details)
                queries.save_participants(conn, case_id, details.get("participants", []))
                queries.save_events(conn, case_id, details.get("events", []))
                queries.log_sync(conn, case_id, True, "api sync")
                return jsonify({"success": True, "message": "synced"})
            queries.log_sync(conn, case_id, False, "scraper None")
            return jsonify({"success": False, "message": "scraper returned no data"})
        except Exception as exc:
            queries.log_sync(conn, case_id, False, str(exc))
            return jsonify({"success": False, "message": str(exc)}), 500

    @app.route("/api/sync/status")
    def api_sync_status():
        conn = _get_db()
        return jsonify({
            "last_sync": _sync_state["last_sync"],
            "next_sync": None,
            "is_running": _sync_state["is_running"],
            "cases_count": conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0],
        })

    @app.route("/api/notifications")
    def api_notifications():
        conn = _get_db()
        rows = conn.execute(
            "SELECT * FROM notifications WHERE is_read=0 ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/notifications/read-all", methods=["POST"])
    def api_notifications_read_all():
        conn = _get_db()
        conn.execute("UPDATE notifications SET is_read=1")
        conn.commit()
        return jsonify({"success": True})

    return app


# ---------------------------------------------------------------------------
# Run directly
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = create_app()
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG, threaded=True)
