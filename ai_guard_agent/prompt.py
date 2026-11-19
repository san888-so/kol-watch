# -*- coding: utf-8 -*-
"""Prompt + schema for the KOL post reviewer. Policy text can be overridden
from KOL Watch (setting ai_review_policy) so the team tunes it without
touching sanmini."""

DEFAULT_POLICY = """ลูกค้าของเราคือ Webull (วีบลู) — แอปโบรกเกอร์ซื้อขายหุ้นต่างประเทศ/หุ้นอเมริกา
KOL กลุ่ม Exclusive เซ็นสัญญาห้ามพูดถึง/โปรโมต/ให้ลิงก์ "คู่แข่ง" ทุกเจ้า

คู่แข่ง = บริษัท/แอป/แพลตฟอร์มใดๆ ที่ให้คนไปเปิดบัญชีลงทุนหรือเทรด นอกจาก Webull เช่น
โบรกเกอร์หุ้นไทย/ต่างประเทศ (บล.ทุกเจ้า, Finansia/HERO/HeroM, InnovestX, Dime!, Liberator, Moomoo, Tiger, IBKR, eToro, Streaming ฯลฯ),
แอปซื้อหุ้นต่างประเทศ/DR/กองทุน, โบรกเกอร์ Forex/ทองคำ/CFD (เช่น HFM, XM, Exness), กระดานคริปโต (Binance, Bitkub ฯลฯ)
รวมถึงแบรนด์ใหม่ที่ไม่อยู่ในรายชื่อด้วย — ให้ตัดสินจากความหมาย ไม่ใช่แค่คำตรงตัว

ไม่ใช่คู่แข่ง: ชื่อหุ้น/บริษัทจดทะเบียนที่พูดถึงในฐานะหุ้นที่วิเคราะห์ (เช่น HANA, NVDA, MICRON),
ชื่อ KOL/ชื่อช่องที่บังเอิญมีคำซ้ำ (เช่น ช่อง "Money Hero"), ธนาคารที่พูดถึงเรื่องเงินฝาก/สินเชื่อทั่วไป

ระดับ:
- urgent = โปรโมต/แนะนำ/รีวิว/ให้ลิงก์ หรือรหัสแนะนำ/บอกว่าใช้ หรือไปงานของคู่แข่ง หรือชวนเปิดบัญชีกับคู่แข่ง หรือสอนเทรดหุ้นต่างประเทศผ่านช่องทางที่ไม่ใช่ Webull
- watch = เอ่ยถึงคู่แข่งแบบไม่ได้โปรโมต (อ้างบทวิเคราะห์ "source: yuanta research", ข่าว, เปรียบเทียบเฉยๆ) หรือพูดเรื่องวิธีซื้อ/เปิดบัญชีหุ้นต่างประเทศโดยไม่บอกว่าผ่าน Webull
- none = ไม่เกี่ยวกับคู่แข่ง (วิเคราะห์หุ้นทั่วไป รวมถึงวิเคราะห์หุ้นอเมริกาเฉยๆ, โพสต์ Webull, ไลฟ์สไตล์)
ถ้าโพสต์พูดถึง Webull แต่ก็โปรโมตคู่แข่งด้วย ยังนับเป็น urgent
ธนาคารที่พูดถึงเรื่องเงินฝาก/ดอกเบี้ย/บัตร/สินเชื่อ ไม่ใช่คู่แข่ง (นับเฉพาะบริษัทหลักทรัพย์/แอปเทรดของธนาคาร)"""

SCHEMA = {
    "type": "object",
    "properties": {
        "competitors": {"type": "array", "items": {"type": "string"},
                        "description": "ชื่อแบรนด์คู่แข่งที่ถูกพูดถึง (ชื่อทางการ)"},
        "foreign_stock_trading": {"type": "boolean",
                                  "description": "โพสต์พูดเรื่องการซื้อ/เทรด/เปิดบัญชีหุ้นต่างประเทศ (ไม่ใช่แค่วิเคราะห์หุ้น)"},
        "mentions_webull": {"type": "boolean"},
        "level": {"type": "string", "enum": ["urgent", "watch", "none"]},
        "reason": {"type": "string", "description": "เหตุผลภาษาไทย 1 ประโยคสั้น"},
        "evidence": {"type": "string", "description": "ข้อความจากโพสต์ที่เป็นหลักฐาน คัดมาตรงตัว ไม่เกิน 120 ตัวอักษร"},
    },
    "required": ["competitors", "foreign_stock_trading", "mentions_webull",
                 "level", "reason", "evidence"],
}


def build(policy, known_competitors, post):
    sys = (policy or DEFAULT_POLICY) + "\n\nรายชื่อคู่แข่งที่รู้แล้ว (ไม่ครบ ต้องจับเจ้าใหม่ด้วย): " \
        + ", ".join(known_competitors) + \
        "\n\nตอบเป็น JSON ตาม schema เท่านั้น · competitors ว่าง [] ถ้าไม่มี (ห้ามใส่ 'none') · " \
        "reason และ evidence ต้องเป็นภาษาไทย (evidence คัดจากโพสต์ตรงตัว)"
    tags = " ".join("#" + h for h in (post.get("hashtags") or []))
    user = (f"KOL: {post.get('kol')}  แพลตฟอร์ม: {post.get('platform')}\n"
            f"--- โพสต์ ---\n{(post.get('caption') or '')[:2500]}\n{tags}")
    return sys, user


CONFIRM_SCHEMA = {
    "type": "object",
    "properties": {
        "confirm": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["confirm", "reason"],
}


def build_confirm(post, verdict):
    """Second, stricter look at an 'urgent' verdict to cut false alarms."""
    sys = ("คุณเป็นผู้ตรวจสอบรอบสอง ลูกค้าคือ Webull (แอปเทรดหุ้นต่างประเทศ) "
           "KOL เซ็นสัญญาห้ามโปรโมตคู่แข่ง ผู้ตรวจรอบแรกบอกว่าโพสต์นี้ 'ด่วน' "
           "ให้ตรวจว่าจริงไหม: confirm=true เฉพาะเมื่อโพสต์โปรโมต/แนะนำ/ให้ลิงก์/รหัสแนะนำ/"
           "ชวนเปิดบัญชี/ไปงานของบริษัทหรือแอปลงทุน-เทรดที่ไม่ใช่ Webull "
           "หรือสอนวิธีซื้อหุ้นต่างประเทศผ่านช่องทางอื่นที่ไม่ใช่ Webull จริงๆ · "
           "confirm=false ถ้าเป็นแค่ชื่อหุ้นที่วิเคราะห์ ชื่อช่อง/KOL ข่าว การอ้างอิงแหล่งข้อมูล "
           "หรือโพสต์ Webull เอง · reason ภาษาไทยสั้นๆ ตอบ JSON เท่านั้น")
    user = (f"ผลรอบแรก: คู่แข่ง={verdict.get('competitors')} เหตุผล={verdict.get('reason')}\n"
            f"KOL: {post.get('kol')} ({post.get('platform')})\n--- โพสต์ ---\n"
            f"{(post.get('caption') or '')[:2500]}")
    return sys, user
