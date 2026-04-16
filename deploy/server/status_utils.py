from typing import Any, Optional


def has_status_value(value: Any) -> bool:
    return value is not None and value != ""


def _coalesce(*values: Any) -> Any:
    for value in values:
        if has_status_value(value):
            return value
    return None


def _extract_material_from_tray_block(tray_block: Any) -> Optional[str]:
    if not isinstance(tray_block, dict):
        return None

    material = _coalesce(
        tray_block.get("tray_type"),
        tray_block.get("tray_sub_brands"),
        tray_block.get("tray_info_idx"),
        tray_block.get("tray_id_name"),
    )
    return str(material).strip() if has_status_value(material) else None


def _normalize_slot_ids(*values: Any) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()

    for value in values:
        if not has_status_value(value):
            continue

        slot_id = str(value).strip()
        if not slot_id or slot_id in seen:
            continue

        normalized.append(slot_id)
        seen.add(slot_id)

    return normalized


def _extract_material_from_virtual_slots(print_block: dict[str, Any], payload: dict[str, Any]) -> Optional[str]:
    virtual_slots = print_block.get("vir_slot") or payload.get("vir_slot") or []
    if not isinstance(virtual_slots, list):
        return None

    ams_block = print_block.get("ams") or {}
    if not isinstance(ams_block, dict):
        ams_block = {}

    preferred_slot_ids = _normalize_slot_ids(
        ams_block.get("tray_tar"),
        ams_block.get("tray_now"),
        ams_block.get("tray_pre"),
        print_block.get("tray_tar"),
        print_block.get("tray_now"),
        print_block.get("tray_pre"),
    )

    for preferred_slot_id in preferred_slot_ids:
        for slot in virtual_slots:
            if not isinstance(slot, dict):
                continue
            if str(slot.get("id") or "").strip() != preferred_slot_id:
                continue

            material = _extract_material_from_tray_block(slot)
            if material:
                return material

    for slot in virtual_slots:
        material = _extract_material_from_tray_block(slot)
        if material:
            return material

    return None


def extract_loaded_material_from_payload(
    payload: Optional[dict[str, Any]],
    *,
    cached_status: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    payload = payload or {}
    cached_status = cached_status or {}
    print_block = payload.get("print") or {}
    if not isinstance(print_block, dict):
        print_block = {}

    for tray_block in (
        print_block.get("vt_tray"),
        print_block.get("tray"),
        payload.get("tray"),
        payload.get("vt_tray"),
    ):
        material = _extract_material_from_tray_block(tray_block)
        if material:
            return material

    material = _extract_material_from_virtual_slots(print_block, payload)
    if material:
        return material

    cached_material = cached_status.get("loaded_material")
    return str(cached_material).strip() if has_status_value(cached_material) else None
