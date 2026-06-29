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


def _save_events_and_deadlines(conn, case_id: int, events: list) -> None:
    """Save events then auto-create deadline entries for the case."""
    queries.save_events(conn, case_id, events)
    try:
        from court_tracker.services.deadline_service import auto_create_deadlines_for_case
        auto_create_deadlines_for_case(conn, case_id)
    except Exception as exc:
        logger.warning("auto_create_deadlines error case %s: %s", case_id, exc)

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
                        _save_events_and_deadlines(conn, cid, details.get("events", []))
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

    @app.template_filter("fmt_money")
    def fmt_money(v) -> str:
        if v is None:
            return "—"
        try:
            f = float(v)
            if f == 0:
                return "—"
            return f"{f:,.0f} ₽".replace(",", " ")
        except (TypeError, ValueError):
            return "—"

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
        expiring_poa = queries.get_expiring_powers_of_attorney(conn, days=30)

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

        fin = queries.get_dashboard_financials(conn)
        return render_template(
            "index.html",
            total_active=len(active),
            new_this_month=len(new_this_month),
            kad_count=kad_count,
            soy_count=soy_count,
            upcoming_hearings=upcoming_hearings,
            approaching_deadlines=approaching_deadlines,
            expiring_poa=expiring_poa,
            recent_logs=recent_logs,
            kanban_counts=kanban_counts,
            total_active_claims=fin["total_active_claims"],
            fee_receivable=fin["fee_receivable"],
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
                    _save_events_and_deadlines(conn, case_id, details.get("events", []))
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
                _save_events_and_deadlines(conn, case_id, details.get("events", []))
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
                _save_events_and_deadlines(conn, case_id, details.get("events", []))
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

    # ------------------------------------------------------------------
    # Clients — HTML routes
    # ------------------------------------------------------------------

    @app.route("/clients")
    def clients_list():
        conn = _get_db()
        clients = queries.get_all_clients(conn)
        return render_template("clients.html", clients=clients)

    @app.route("/clients/add", methods=["GET", "POST"])
    def client_add():
        if request.method == "POST":
            conn = _get_db()
            f = request.form
            data = {
                "type":           f.get("type", "legal"),
                "name":           f.get("name", "").strip(),
                "short_name":     f.get("short_name", "").strip() or None,
                "inn":            f.get("inn", "").strip() or None,
                "ogrn":           f.get("ogrn", "").strip() or None,
                "address":        f.get("address", "").strip() or None,
                "status_egrul":   f.get("status_egrul") or None,
                "phone":          f.get("phone", "").strip() or None,
                "email":          f.get("email", "").strip() or None,
                "contact_person": f.get("contact_person", "").strip() or None,
                "notes":          f.get("notes", "").strip() or None,
            }
            if not data["name"]:
                flash("Укажите наименование клиента.", "danger")
                return render_template("client_form.html", client=None, action="add")
            client_id = queries.upsert_client(conn, data)
            flash("Клиент добавлен.", "success")
            return redirect(url_for("client_detail", client_id=client_id))
        return render_template("client_form.html", client=None, action="add")

    @app.route("/clients/<int:client_id>")
    def client_detail(client_id: int):
        conn = _get_db()
        client = queries.get_client_with_cases(conn, client_id)
        if not client:
            flash("Клиент не найден.", "danger")
            return redirect(url_for("clients_list"))
        all_cases = queries.get_all_cases(conn)
        return render_template("client_detail.html", client=client, all_cases=all_cases)

    @app.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
    def client_edit(client_id: int):
        conn = _get_db()
        client = queries.get_client_with_cases(conn, client_id)
        if not client:
            flash("Клиент не найден.", "danger")
            return redirect(url_for("clients_list"))
        if request.method == "POST":
            f = request.form
            data = {
                "type":           f.get("type", client["type"]),
                "name":           f.get("name", "").strip() or client["name"],
                "short_name":     f.get("short_name", "").strip() or None,
                "inn":            client["inn"],  # INN is the upsert key — don't change
                "ogrn":           f.get("ogrn", "").strip() or None,
                "address":        f.get("address", "").strip() or None,
                "status_egrul":   f.get("status_egrul") or None,
                "phone":          f.get("phone", "").strip() or None,
                "email":          f.get("email", "").strip() or None,
                "contact_person": f.get("contact_person", "").strip() or None,
                "notes":          f.get("notes", "").strip() or None,
            }
            # upsert_client matches by INN; fall back to direct UPDATE when INN is missing
            if data["inn"]:
                queries.upsert_client(conn, data)
            else:
                now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
                conn.execute(
                    """UPDATE clients SET type=?,name=?,short_name=?,ogrn=?,address=?,
                       status_egrul=?,phone=?,email=?,contact_person=?,notes=?,updated_at=?
                       WHERE id=?""",
                    (data["type"], data["name"], data["short_name"], data["ogrn"],
                     data["address"], data["status_egrul"], data["phone"], data["email"],
                     data["contact_person"], data["notes"], now, client_id),
                )
                conn.commit()
            flash("Клиент обновлён.", "success")
            return redirect(url_for("client_detail", client_id=client_id))
        return render_template("client_form.html", client=client, action="edit")

    @app.route("/clients/<int:client_id>/delete", methods=["POST"])
    def client_delete(client_id: int):
        conn = _get_db()
        active = conn.execute(
            "SELECT COUNT(*) FROM cases WHERE client_id=? AND status NOT IN ('Завершено','Прекращено')",
            (client_id,),
        ).fetchone()[0]
        if active > 0:
            flash(f"Нельзя удалить клиента: есть {active} активных дел.", "danger")
            return redirect(url_for("client_detail", client_id=client_id))
        conn.execute("DELETE FROM clients WHERE id=?", (client_id,))
        conn.commit()
        flash("Клиент удалён.", "success")
        return redirect(url_for("clients_list"))

    # ------------------------------------------------------------------
    # Client contacts
    # ------------------------------------------------------------------

    @app.route("/clients/<int:client_id>/contacts/add", methods=["POST"])
    def contact_add(client_id: int):
        conn = _get_db()
        f = request.form
        queries.add_contact(conn, {
            "client_id": client_id,
            "name":  f.get("name", "").strip(),
            "role":  f.get("role", "").strip(),
            "phone": f.get("phone", "").strip(),
            "email": f.get("email", "").strip(),
            "notes": f.get("notes", "").strip(),
        })
        flash("Контакт добавлен.", "success")
        return redirect(url_for("client_detail", client_id=client_id) + "#tab-contacts")

    @app.route("/clients/<int:client_id>/contacts/<int:contact_id>/delete", methods=["POST"])
    def contact_delete(client_id: int, contact_id: int):
        conn = _get_db()
        queries.delete_contact(conn, contact_id)
        flash("Контакт удалён.", "success")
        return redirect(url_for("client_detail", client_id=client_id) + "#tab-contacts")

    # ------------------------------------------------------------------
    # Powers of attorney
    # ------------------------------------------------------------------

    @app.route("/clients/<int:client_id>/poa/add", methods=["POST"])
    def poa_add(client_id: int):
        conn = _get_db()
        f = request.form
        file = request.files.get("scan")
        attachment_id = None
        if file and file.filename:
            import os, uuid
            from court_tracker.config import ATTACHMENTS_DIR
            ext = os.path.splitext(file.filename)[1]
            stored = f"poa_{uuid.uuid4().hex}{ext}"
            dest = ATTACHMENTS_DIR / stored
            file.save(str(dest))
            # We need a case_id for attachments FK — use NULL pattern via dummy case
            # PoA scans are stored with case_id=0 sentinel (FK allows NULL, use 0 workaround)
            # Actually the schema has case_id NOT NULL; store against the first linked case or skip
            # For now: find any case linked to this client
            row = conn.execute("SELECT id FROM cases WHERE client_id=? LIMIT 1", (client_id,)).fetchone()
            if row:
                cur = conn.execute(
                    """INSERT INTO attachments(case_id,filename,stored_name,file_path,file_size,mime_type)
                       VALUES (?,?,?,?,?,?)""",
                    (row[0], file.filename, stored, str(dest),
                     os.path.getsize(str(dest)), file.content_type),
                )
                attachment_id = cur.lastrowid
                conn.commit()
        queries.add_power_of_attorney(conn, {
            "client_id":        client_id,
            "case_id":          f.get("case_id") or None,
            "number":           f.get("number", "").strip() or None,
            "issue_date":       f.get("issue_date") or None,
            "expiry_date":      f.get("expiry_date") or None,
            "scope_description":f.get("scope_description", "").strip() or None,
            "attachment_id":    attachment_id,
        })
        flash("Доверенность добавлена.", "success")
        return redirect(url_for("client_detail", client_id=client_id) + "#tab-poa")

    @app.route("/clients/<int:client_id>/poa/<int:poa_id>/delete", methods=["POST"])
    def poa_delete(client_id: int, poa_id: int):
        conn = _get_db()
        queries.delete_power_of_attorney(conn, poa_id)
        flash("Доверенность удалена.", "success")
        return redirect(url_for("client_detail", client_id=client_id) + "#tab-poa")

    # ------------------------------------------------------------------
    # Client JSON API
    # ------------------------------------------------------------------

    @app.route("/api/clients")
    def api_clients():
        conn = _get_db()
        return jsonify(queries.get_all_clients(conn))

    @app.route("/api/clients", methods=["POST"])
    def api_client_create():
        conn = _get_db()
        data = request.get_json(force=True, silent=True) or {}
        if not data.get("name"):
            return jsonify({"error": "name required"}), 400
        client_id = queries.upsert_client(conn, data)
        client = queries.get_client_with_cases(conn, client_id)
        return jsonify(dict(client)), 201

    @app.route("/api/egrul/<inn>")
    def api_egrul(inn: str):
        from court_tracker.services.egrul_service import fetch_by_inn
        data = fetch_by_inn(inn)
        if data is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(data)

    @app.route("/api/cases/<int:case_id>/set-client", methods=["POST"])
    def api_set_client(case_id: int):
        conn = _get_db()
        data = request.get_json(force=True, silent=True) or {}
        client_id = data.get("client_id")
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute(
            "UPDATE cases SET client_id=?, updated_at=? WHERE id=?",
            (client_id, now, case_id),
        )
        conn.commit()
        return jsonify({"success": True})

    # ------------------------------------------------------------------
    # Deadlines API (Phase 4)
    # ------------------------------------------------------------------

    @app.route("/api/cases/<int:case_id>/deadlines")
    def api_case_deadlines(case_id: int):
        from court_tracker.services.deadline_service import urgency_level, DEADLINE_RULES
        conn = _get_db()
        deadlines = queries.get_deadlines_for_case(conn, case_id)
        for d in deadlines:
            d["urgency"] = urgency_level(d.get("deadline_date"))
        return jsonify(deadlines)

    @app.route("/api/cases/<int:case_id>/deadlines", methods=["POST"])
    def api_create_deadline(case_id: int):
        from court_tracker.services.deadline_service import (
            calculate_deadline, DEADLINE_RULES
        )
        conn = _get_db()
        data = request.get_json(force=True, silent=True) or {}

        # If a rule_key + trigger_date are provided, auto-calculate
        rule_key = data.get("rule_key")
        trigger_date_str = data.get("trigger_date")
        if rule_key and trigger_date_str and rule_key in DEADLINE_RULES:
            from datetime import date as _date
            try:
                td = _date.fromisoformat(trigger_date_str[:10])
            except ValueError:
                return jsonify({"error": "invalid trigger_date"}), 400
            dl_date = calculate_deadline(td, rule_key)
            rule = DEADLINE_RULES[rule_key]
            data.setdefault("deadline_type", rule["name"])
            data.setdefault("statute_reference", rule["statute_reference"])
            data.setdefault("statute_article", rule["statute_article"])
            data.setdefault("calculation_note", rule["calculation_note"])
            data.setdefault("deadline_date", dl_date.isoformat() if dl_date else None)
            data["is_auto_calculated"] = 1
            data["trigger_event"] = rule["trigger_event"]

        data["case_id"] = case_id
        if not data.get("deadline_date"):
            return jsonify({"error": "deadline_date required"}), 400

        dl_id = queries.create_deadline(conn, data)
        return jsonify({"success": True, "id": dl_id, "deadline_date": data["deadline_date"]}), 201

    @app.route("/api/deadlines/<int:dl_id>", methods=["PATCH"])
    def api_update_deadline(dl_id: int):
        conn = _get_db()
        data = request.get_json(force=True, silent=True) or {}
        new_date = data.get("deadline_date")
        if not new_date:
            return jsonify({"error": "deadline_date required"}), 400
        ok = queries.update_deadline_date(conn, dl_id, new_date)
        if not ok:
            return jsonify({"error": "not found"}), 404
        return jsonify({"success": True, "new_date": new_date, "calculation_note": "изменено вручную"})

    @app.route("/api/deadlines/<int:dl_id>", methods=["DELETE"])
    def api_delete_deadline(dl_id: int):
        conn = _get_db()
        ok = queries.delete_deadline(conn, dl_id)
        if not ok:
            return jsonify({"error": "not found"}), 404
        return jsonify({"success": True})

    @app.route("/api/deadlines/<int:dl_id>/done", methods=["PATCH"])
    def api_toggle_deadline_done(dl_id: int):
        conn = _get_db()
        new_state = queries.toggle_deadline_done(conn, dl_id)
        if new_state is None:
            return jsonify({"error": "not found"}), 404
        return jsonify({"success": True, "is_done": new_state})

    # Helper: expose DEADLINE_RULES to front-end
    @app.route("/api/deadline-rules")
    def api_deadline_rules():
        from court_tracker.services.deadline_service import DEADLINE_RULES
        return jsonify(DEADLINE_RULES)

    # ------------------------------------------------------------------
    # Kanban (Phase 4)
    # ------------------------------------------------------------------

    @app.route("/kanban")
    def kanban():
        conn = _get_db()
        board = queries.get_kanban_board(conn)
        # Attach critical deadline dot info
        for stage, cases in board.items():
            for c in cases:
                c["critical"] = bool(queries.get_case_critical_deadline(conn, c["id"]))
        return render_template(
            "kanban.html",
            board=board,
            stage_names=queries.STAGE_NAMES,
            stages=queries.KANBAN_STAGES,
        )

    @app.route("/api/kanban")
    def api_kanban():
        conn = _get_db()
        board = queries.get_kanban_board(conn)
        return jsonify(board)

    @app.route("/api/kanban/move", methods=["POST"])
    def api_kanban_move():
        conn = _get_db()
        data = request.get_json(force=True, silent=True) or {}
        case_id = data.get("case_id")
        new_stage = data.get("new_stage")
        if not case_id or not new_stage:
            return jsonify({"error": "case_id and new_stage required"}), 400
        if new_stage not in queries.KANBAN_STAGES:
            return jsonify({"error": f"invalid stage: {new_stage}"}), 400
        warning = queries.move_kanban(conn, int(case_id), new_stage)
        resp = {"success": True}
        if warning:
            resp["mismatch_warning"] = warning
        return jsonify(resp)

    # ------------------------------------------------------------------
    # Notes & Tasks (Phase 5)
    # ------------------------------------------------------------------

    @app.route("/api/cases/<int:case_id>/notes")
    def api_get_notes(case_id: int):
        conn = _get_db()
        return jsonify(queries.get_notes_for_case(conn, case_id))

    @app.route("/api/cases/<int:case_id>/notes", methods=["POST"])
    def api_create_note(case_id: int):
        conn = _get_db()
        data = request.get_json(force=True, silent=True) or {}
        data["case_id"] = case_id
        note_id = queries.create_note(conn, data)
        all_notes = queries.get_notes_for_case(conn, case_id)
        new_note = next((n for n in all_notes if n["id"] == note_id),
                        {"id": note_id, "item_type": data.get("item_type", "note"),
                         "checklist": [], "tags": []})
        return jsonify(new_note), 201

    @app.route("/api/notes/<int:note_id>", methods=["PUT"])
    def api_update_note(note_id: int):
        conn = _get_db()
        data = request.get_json(force=True, silent=True) or {}
        queries.update_note(conn, note_id, data)
        return jsonify({"success": True})

    @app.route("/api/notes/<int:note_id>", methods=["DELETE"])
    def api_delete_note(note_id: int):
        conn = _get_db()
        ok = queries.delete_note(conn, note_id)
        if not ok:
            return jsonify({"error": "not found"}), 404
        return jsonify({"success": True})

    @app.route("/api/notes/<int:note_id>/checklist", methods=["POST"])
    def api_add_checklist(note_id: int):
        conn = _get_db()
        data = request.get_json(force=True, silent=True) or {}
        text = (data.get("text") or "").strip()
        if not text:
            return jsonify({"error": "text required"}), 400
        item_id = queries.add_checklist_item(conn, note_id, text)
        return jsonify({"success": True, "id": item_id, "text": text,
                        "checked": False, "position": 0}), 201

    @app.route("/api/checklist/<int:item_id>/toggle", methods=["PATCH"])
    def api_toggle_checklist(item_id: int):
        conn = _get_db()
        new_state = queries.toggle_checklist_item(conn, item_id)
        if new_state is None:
            return jsonify({"error": "not found"}), 404
        return jsonify({"success": True, "checked": new_state})

    @app.route("/api/checklist/<int:item_id>", methods=["DELETE"])
    def api_delete_checklist(item_id: int):
        conn = _get_db()
        ok = queries.delete_checklist_item(conn, item_id)
        if not ok:
            return jsonify({"error": "not found"}), 404
        return jsonify({"success": True})

    @app.route("/api/notes/<int:note_id>/tags", methods=["POST"])
    def api_add_tag(note_id: int):
        conn = _get_db()
        data = request.get_json(force=True, silent=True) or {}
        tag = (data.get("tag") or "").strip()
        if not tag:
            return jsonify({"error": "tag required"}), 400
        queries.add_note_tag(conn, note_id, tag)
        return jsonify({"success": True, "tag": tag})

    @app.route("/api/notes/<int:note_id>/tags", methods=["DELETE"])
    def api_delete_tag(note_id: int):
        conn = _get_db()
        data = request.get_json(force=True, silent=True) or {}
        tag = (data.get("tag") or "").strip()
        if not tag:
            return jsonify({"error": "tag required"}), 400
        queries.delete_note_tag(conn, note_id, tag)
        return jsonify({"success": True})

    @app.route("/api/tags")
    def api_tags():
        conn = _get_db()
        return jsonify(queries.get_all_tags(conn))

    @app.route("/api/notes/reorder", methods=["POST"])
    def api_reorder_notes():
        conn = _get_db()
        data = request.get_json(force=True, silent=True) or {}
        case_id = data.get("case_id")
        ordered_ids = data.get("ordered_ids", [])
        if not case_id or not ordered_ids:
            return jsonify({"error": "case_id and ordered_ids required"}), 400
        queries.reorder_notes(conn, int(case_id), [int(i) for i in ordered_ids])
        return jsonify({"success": True})

    # ------------------------------------------------------------------
    # Attachments (Phase 5)
    # ------------------------------------------------------------------

    from court_tracker.config import ALLOWED_EXTENSIONS, MAX_FILE_SIZE_MB

    def _allowed_file(filename: str) -> bool:
        return "." in filename and \
               filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

    @app.route("/api/cases/<int:case_id>/attachments")
    def api_get_attachments(case_id: int):
        conn = _get_db()
        return jsonify(queries.get_attachments_for_case(conn, case_id))

    @app.route("/api/cases/<int:case_id>/attachments", methods=["POST"])
    def api_upload_attachment(case_id: int):
        import os
        import uuid
        from flask import send_file as _sf
        from court_tracker.config import ATTACHMENTS_DIR

        conn = _get_db()
        case = queries.get_case_full(conn, case_id)
        if not case:
            return jsonify({"error": "case not found"}), 404

        if "file" not in request.files:
            return jsonify({"error": "no file"}), 400
        file = request.files["file"]
        if not file.filename:
            return jsonify({"error": "empty filename"}), 400
        if not _allowed_file(file.filename):
            return jsonify({"error": "file type not allowed"}), 400

        file.seek(0, 2)
        size = file.tell()
        file.seek(0)
        if size > MAX_FILE_SIZE_MB * 1024 * 1024:
            return jsonify({"error": f"file too large (max {MAX_FILE_SIZE_MB} MB)"}), 400

        ext = os.path.splitext(file.filename)[1].lower()
        stored = f"{uuid.uuid4().hex}{ext}"
        dest = ATTACHMENTS_DIR / stored
        file.save(str(dest))

        note_id_raw = request.form.get("note_id")
        note_id = int(note_id_raw) if note_id_raw else None

        att_id = queries.create_attachment(conn, {
            "case_id":     case_id,
            "note_id":     note_id,
            "filename":    file.filename,
            "stored_name": stored,
            "file_path":   str(dest),
            "file_size":   size,
            "mime_type":   file.content_type,
        })
        all_att = queries.get_attachments_for_case(conn, case_id)
        att = next((a for a in all_att if a["id"] == att_id), {"id": att_id})
        return jsonify(att), 201

    @app.route("/api/cases/<int:case_id>/attachments/<int:att_id>/download")
    def api_download_attachment(case_id: int, att_id: int):
        from flask import send_file as _sf
        conn = _get_db()
        row = conn.execute(
            "SELECT * FROM attachments WHERE id=? AND case_id=?", (att_id, case_id)
        ).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        att = dict(row)
        return _sf(att["file_path"], as_attachment=True,
                   download_name=att["filename"])

    @app.route("/api/attachments/<int:att_id>", methods=["DELETE"])
    def api_delete_attachment(att_id: int):
        import os
        conn = _get_db()
        file_path = queries.delete_attachment(conn, att_id)
        if not file_path:
            return jsonify({"error": "not found"}), 404
        try:
            os.remove(file_path)
        except OSError:
            pass
        return jsonify({"success": True})

    # ------------------------------------------------------------------
    # Analytics (Phase 6)
    # ------------------------------------------------------------------

    @app.route("/analytics")
    def analytics():
        return render_template("analytics.html")

    @app.route("/api/analytics/cases")
    def api_analytics_cases():
        conn = _get_db()
        return jsonify(queries.get_analytics_cases(conn))

    @app.route("/api/analytics/judges")
    def api_analytics_judges():
        conn = _get_db()
        return jsonify(queries.get_analytics_judges(conn))

    @app.route("/api/analytics/finance")
    def api_analytics_finance():
        conn = _get_db()
        return jsonify(queries.get_analytics_finance(conn))

    # ------------------------------------------------------------------
    # Calendar (Phase 6)
    # ------------------------------------------------------------------

    @app.route("/calendar")
    def calendar():
        return render_template("calendar.html")

    @app.route("/api/calendar")
    def api_calendar():
        conn = _get_db()
        try:
            year  = int(request.args.get("year",  datetime.utcnow().year))
            month = int(request.args.get("month", datetime.utcnow().month))
        except ValueError:
            return jsonify({"error": "invalid year/month"}), 400
        if not (1 <= month <= 12):
            return jsonify({"error": "month out of range"}), 400
        return jsonify(queries.get_calendar_events(conn, year, month))

    # ------------------------------------------------------------------
    # Document Templates (Phase 5)
    # ------------------------------------------------------------------

    @app.route("/templates")
    def templates_list():
        import os
        from court_tracker.config import TEMPLATES_DIR
        files = sorted(
            f for f in os.listdir(str(TEMPLATES_DIR))
            if f.lower().endswith(".docx")
        )
        return jsonify([{"filename": f} for f in files])

    @app.route("/templates/generate", methods=["POST"])
    def templates_generate():
        import os
        import uuid
        from flask import send_file as _sf
        from court_tracker.config import TEMPLATES_DIR, TEMP_DIR

        data = request.get_json(force=True, silent=True) or {}
        template_filename = data.get("template_filename", "")
        case_id = data.get("case_id")

        if not template_filename or not case_id:
            return jsonify({"error": "template_filename and case_id required"}), 400
        if "/" in template_filename or "\\" in template_filename \
                or not template_filename.lower().endswith(".docx"):
            return jsonify({"error": "invalid filename"}), 400

        template_path = TEMPLATES_DIR / template_filename
        if not template_path.exists():
            return jsonify({"error": "template not found"}), 404

        conn = _get_db()
        case = queries.get_case_full(conn, int(case_id))
        if not case:
            return jsonify({"error": "case not found"}), 404

        participants = case.get("participants", [])
        plaintiff  = next(
            (p["name"] for p in participants
             if p.get("role") and "истец" in p["role"].lower()), "")
        respondent = next(
            (p["name"] for p in participants
             if p.get("role") and "ответчик" in p["role"].lower()), "")
        client_name = ""
        if case.get("client_id"):
            cl_row = conn.execute(
                "SELECT name FROM clients WHERE id=?", (case["client_id"],)
            ).fetchone()
            if cl_row:
                client_name = cl_row[0]

        placeholders = {
            "{{номер_дела}}": case.get("case_number") or "",
            "{{суд}}":        case.get("court") or "",
            "{{судья}}":      case.get("judge") or "",
            "{{истец}}":      plaintiff,
            "{{ответчик}}":   respondent,
            "{{дата}}":       datetime.utcnow().strftime("%d.%m.%Y"),
            "{{клиент}}":     client_name,
            "{{сумма_иска}}": "",
        }

        def _replace_in_para(para):
            for key, val in placeholders.items():
                if key in para.text:
                    for run in para.runs:
                        if key in run.text:
                            run.text = run.text.replace(key, val)

        try:
            from docx import Document as _DocxDocument
            doc = _DocxDocument(str(template_path))
            for para in doc.paragraphs:
                _replace_in_para(para)
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        for para in cell.paragraphs:
                            _replace_in_para(para)
            out_name = f"{uuid.uuid4().hex}_{template_filename}"
            out_path = TEMP_DIR / out_name
            doc.save(str(out_path))
        except Exception as exc:
            return jsonify({"error": f"generation failed: {exc}"}), 500

        from flask import send_file as _sf
        safe_case_number = (case.get("case_number") or "case").replace("/", "-")
        return _sf(str(out_path), as_attachment=True,
                   download_name=f"{safe_case_number}_{template_filename}")

    return app


# ---------------------------------------------------------------------------
# Run directly
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = create_app()
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG, threaded=True)
