"""Portal group view over Worker-compatible, per-employee schedules."""
from copy import deepcopy
import logging
from uuid import uuid4


class GroupScheduleStore:
    def __init__(self, store):
        self.store = store

    def close(self):
        self.store.close()

    def _members(self, schedule_id):
        document = self.store.get_schedule(schedule_id)
        if document is None:
            return []
        group = document.get("group_id")
        if not group:
            return [document]
        return [d for d in self.store.list_schedules() if d.get("group_id") == group]

    @staticmethod
    def _view(members):
        first = min(members, key=lambda d: str(d["_id"]))
        group_id = first.get("group_id", first["_id"])
        first = next((d for d in members if d["_id"] == group_id), first)
        result = deepcopy(first)
        result["_id"] = group_id
        result["owner_id"] = first.get("registrant_id", first["owner_id"])
        result["owner_name"] = first.get("registrant_name", first.get("owner_name", ""))
        result["recipient_ids"] = sorted({d["owner_id"] for d in members})
        result["execution_members"] = [
            {"schedule_id": str(d["_id"]), "employee_id": d["owner_id"],
             "status": d.get("status", "inactive"),
             "next_run_at": d.get("next_run_at", ""),
             "last_run_status": d.get("last_run_status", ""),
             "last_run_at": d.get("last_run_at", "")}
            for d in members
        ]
        active = [d for d in members if d.get("status") == "active"]
        result["status"] = "active" if active else "inactive"
        if active:
            result["next_run_at"] = min((d.get("next_run_at") or "" for d in active))
        return result

    def list_schedules(self):
        groups = {}
        for document in self.store.list_schedules():
            groups.setdefault(document.get("group_id", document["_id"]), []).append(document)
        return [self._view(members) for members in groups.values()]

    def get_schedule(self, schedule_id):
        members = self._members(schedule_id)
        return self._view(members) if members else None

    def _pause_after_failure(self, ids):
        for identifier in ids:
            try:
                self.store.update_schedule(identifier, {"status": "inactive", "next_run_at": None})
            except Exception:
                logging.getLogger(__name__).exception("Could not pause schedule after a group write failure: %s", identifier)

    def create_schedule(self, document):
        recipients = document.get("recipient_ids") or [document["owner_id"]]
        ids = []
        try:
            for index, employee in enumerate(recipients):
                child = deepcopy(document)
                child.pop("recipient_ids", None)
                child.update(
                    _id=document["_id"] if index == 0 else "SCH-" + str(uuid4()),
                    group_id=document["_id"], registrant_id=document["owner_id"],
                    registrant_name=document.get("owner_name", ""), owner_id=employee,
                    owner_name=document.get("owner_name", "") if employee == document["owner_id"] else "",
                    status="inactive", next_run_at=None,
                )
                ids.append(child["_id"])
                self.store.create_schedule(child)
            for identifier in ids:
                self.store.update_schedule(identifier, {
                    "status": document["status"], "next_run_at": document.get("next_run_at"),
                })
        except Exception:
            self._pause_after_failure(ids)
            raise
        return self.get_schedule(document["_id"])

    def update_schedule(self, schedule_id, update):
        members = self._members(schedule_id)
        if not members:
            return None
        view = self._view(members)
        recipients = update.get("recipient_ids") or view["recipient_ids"]
        fields = deepcopy(update)
        fields.pop("recipient_ids", None)
        fields.pop("owner_name", None)
        existing = {d["owner_id"]: d for d in members}
        ids = [d["_id"] for d in members]
        try:
            # Invalidate existing claims before changing membership or timing.
            for identifier in ids:
                self.store.update_schedule(identifier, {"status": "inactive", "next_run_at": None})
            for employee in recipients:
                if employee not in existing:
                    child = {k: deepcopy(v) for k, v in view.items()
                             if not k.startswith("scheduler_") and not k.startswith("last_run")
                             and k not in ("execution_members", "recipient_ids")}
                    child.update(_id="SCH-" + str(uuid4()), group_id=view["_id"],
                                 registrant_id=view["owner_id"], registrant_name=view.get("owner_name", ""),
                                 owner_id=employee, owner_name="", status="inactive", next_run_at=None)
                    ids.append(child["_id"])
                    existing[employee] = self.store.create_schedule(child)
            # Keep the stable group ID on a remaining execution document.
            anchor = next((d for d in members if d["_id"] == view["_id"]), None)
            if anchor and anchor["owner_id"] not in recipients:
                replacement = existing[recipients[0]]
                self.store.delete_schedule(replacement["_id"])
                self.store.update_schedule(anchor["_id"], {"owner_id": recipients[0], "owner_name": "",
                                                          "last_run_status": "", "last_run_at": None})
                existing[recipients[0]] = {**anchor, "owner_id": recipients[0]}
            for employee in recipients:
                self.store.update_schedule(existing[employee]["_id"], {
                    **fields, "group_id": view["_id"], "registrant_id": view["owner_id"],
                    "registrant_name": view.get("owner_name", ""),
                })
            retained = {existing[e]["_id"] for e in recipients}
            for identifier in ids:
                if identifier not in retained:
                    self.store.delete_schedule(identifier)
        except Exception:
            self._pause_after_failure(ids)
            raise
        return self.get_schedule(view["_id"])

    def delete_schedule(self, schedule_id):
        members = self._members(schedule_id)
        for child in members:
            self.store.update_schedule(child["_id"], {"status": "inactive", "next_run_at": None})
        for child in members:
            self.store.delete_schedule(child["_id"])
        return bool(members)
