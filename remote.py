import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
from datetime import datetime, timezone, timedelta
from streamlit_gsheets import GSheetsConnection
import io
import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import json
import os
import re
from urllib.parse import parse_qs, unquote, urlparse

# カレンダーのUIを強制的に日本語（日本地域）にする設定
os.environ["LC_ALL"] = "ja_JP.UTF-8"
os.environ["LANG"] = "ja_JP.UTF-8"

st.set_page_config(page_title="動物遠隔診療MVP", layout="wide")

st.title("遠隔診療システム")

# Googleスプレッドシートへの接続設定
conn = st.connection("gsheets", type=GSheetsConnection)

SHEET_READ_TTL = 60
RECORDS_CACHE_KEY = "cached_records_df"
NOTIFICATION_CACHE_KEY = "cached_notification_settings"

NOTIFICATION_SETTINGS_KEYS = [
    "LINE_User_ID",
    "LINE通知",
    "現場_LINE_User_ID",
    "現場_LINE通知",
]

DEFAULT_NOTIFICATION_SETTINGS = {
    "LINE_User_ID": "",
    "LINE通知": "無効",
    "現場_LINE_User_ID": "",
    "現場_LINE通知": "無効",
}

DEPARTMENT_OPTIONS = [
    "ベビー室",
    "育成舎",
    "搾乳舎",
    "干乳舎",
    "病牛舎",
    "その他",
]

CHECK_ITEM_OPTIONS = [
    "食欲不振",
    "歩行異常",
    "出血",
    "下痢・嘔吐",
    "ぐったりしている",
    "その他",
]

QR_DEFAULTS_SESSION_KEY = "management_seal_qr_defaults"

# -------------------------------------------------------------------------
# 通知設定の読み書き
# -------------------------------------------------------------------------
def clear_sheet_cache(*, records=True, notifications=True):
    if records:
        st.session_state.pop(RECORDS_CACHE_KEY, None)
    if notifications:
        st.session_state.pop(NOTIFICATION_CACHE_KEY, None)


def sheet_read_error_message(error):
    message = str(error)
    if "429" in message or "RESOURCE_EXHAUSTED" in message or "RATE_LIMIT_EXCEEDED" in message:
        return (
            "Googleスプレッドシートへの読み取りが多すぎます。"
            "1分ほど待ってからページを再読み込みしてください。"
        )
    return f"データ読み込みエラー: {error}"


def load_notification_settings(*, refresh=False):
    if refresh:
        st.session_state.pop(NOTIFICATION_CACHE_KEY, None)
    if NOTIFICATION_CACHE_KEY in st.session_state:
        return st.session_state[NOTIFICATION_CACHE_KEY].copy()

    try:
        df = conn.read(worksheet="通知設定", ttl=SHEET_READ_TTL)
        df = df.fillna("")
        settings = DEFAULT_NOTIFICATION_SETTINGS.copy()
        for _, row in df.iterrows():
            key = str(row.get("キー", "")).strip()
            if key in settings:
                settings[key] = str(row.get("値", "")).strip()
        st.session_state[NOTIFICATION_CACHE_KEY] = settings
        return settings.copy()
    except Exception:
        return DEFAULT_NOTIFICATION_SETTINGS.copy()


def save_notification_settings(settings):
    rows = [{"キー": key, "値": settings.get(key, "")} for key in NOTIFICATION_SETTINGS_KEYS]
    df = pd.DataFrame(rows)
    conn.update(worksheet="通知設定", data=df)
    st.session_state[NOTIFICATION_CACHE_KEY] = settings.copy()


def get_app_url():
    line_config = st.secrets.get("line", {})
    app_config = st.secrets.get("app", {})
    return (line_config.get("app_url") or app_config.get("url") or "").rstrip("/")


def get_query_param(name):
    value = st.query_params.get(name)
    if isinstance(value, list):
        return value[0] if value else None
    return value


def report_field(report, key, default=""):
    if key not in report:
        return default
    value = report.get(key, default)
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return default
    text = str(value).strip()
    if text.lower() in ("nan", "none"):
        return default
    return text


def parse_birth_date_text(text):
    cleaned = str(text or "").strip()
    if not cleaned:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def normalize_department_name(text):
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    if cleaned in DEPARTMENT_OPTIONS:
        return cleaned
    aliases = {
        "baby": "ベビー室",
        "ベビー": "ベビー室",
        "ベビールーム": "ベビー室",
    }
    lowered = cleaned.lower()
    for alias, department in aliases.items():
        if alias in lowered or alias in cleaned:
            return department
    return cleaned


def normalize_check_items(items):
    normalized = []
    for item in items or []:
        text = str(item).strip()
        if not text:
            continue
        if text in CHECK_ITEM_OPTIONS:
            normalized.append(text)
            continue
        for option in CHECK_ITEM_OPTIONS:
            if option in text:
                normalized.append(option)
                break
    seen = set()
    unique_items = []
    for item in normalized:
        if item not in seen:
            seen.add(item)
            unique_items.append(item)
    return unique_items


def parse_management_seal_qr(raw_text):
    result = {
        "animal_id": "",
        "birth_date": None,
        "department": "",
        "check_items": [],
    }
    text = str(raw_text or "").strip()
    if not text:
        return result

    def apply_mapping(data):
        if not isinstance(data, dict):
            return
        animal_id = (
            data.get("個体識別番号")
            or data.get("個体番号")
            or data.get("animal_id")
            or data.get("id")
            or data.get("no")
        )
        if animal_id:
            result["animal_id"] = str(animal_id).strip()

        birth_text = (
            data.get("生年月日")
            or data.get("birth_date")
            or data.get("birth")
        )
        if birth_text:
            result["birth_date"] = parse_birth_date_text(birth_text)

        department = (
            data.get("部署")
            or data.get("department")
            or data.get("dept")
        )
        if department:
            result["department"] = normalize_department_name(department)

        check_items = data.get("チェック項目") or data.get("check_items") or data.get("checks")
        if isinstance(check_items, str):
            result["check_items"] = normalize_check_items(
                re.split(r"[,、/|]", check_items)
            )
        elif isinstance(check_items, list):
            result["check_items"] = normalize_check_items(check_items)

    if text.startswith("{"):
        try:
            apply_mapping(json.loads(text))
            return result
        except json.JSONDecodeError:
            pass

    if "://" in text or "?" in text:
        candidate = text if "://" in text else f"https://dummy.local/?{text.lstrip('?')}"
        parsed = urlparse(candidate)
        query = parse_qs(parsed.query, keep_blank_values=True)
        flattened = {key: values[0] for key, values in query.items() if values}
        apply_mapping(flattened)
        if result["animal_id"] or result["department"]:
            return result

    if "=" in text and not text.startswith("{"):
        pairs = {}
        for chunk in re.split(r"[;&]", text):
            if "=" not in chunk:
                continue
            key, value = chunk.split("=", 1)
            pairs[unquote(key.strip())] = unquote(value.strip())
        apply_mapping(pairs)
        if result["animal_id"] or result["department"]:
            return result

    if "," in text or "\t" in text:
        parts = [part.strip() for part in re.split(r"[,\t]", text) if part.strip()]
        if parts:
            result["animal_id"] = parts[0]
        if len(parts) > 1:
            result["birth_date"] = parse_birth_date_text(parts[1])
        if len(parts) > 2:
            result["department"] = normalize_department_name(parts[2])
        if len(parts) > 3:
            result["check_items"] = normalize_check_items(parts[3:])
        return result

    result["animal_id"] = text
    return result


def get_qr_form_defaults():
    defaults = st.session_state.get(QR_DEFAULTS_SESSION_KEY, {})
    if not isinstance(defaults, dict):
        return {}
    return defaults


def set_qr_form_defaults(defaults):
    st.session_state[QR_DEFAULTS_SESSION_KEY] = defaults


def apply_management_seal_query_params():
    query_text = get_query_param("qr")
    if query_text:
        set_qr_form_defaults(parse_management_seal_qr(query_text))
        return

    direct_params = {
        "animal_id": get_query_param("animal_id") or get_query_param("id"),
        "birth_date": get_query_param("birth_date") or get_query_param("birth"),
        "department": get_query_param("department") or get_query_param("dept"),
        "check_items": get_query_param("check_items") or get_query_param("checks"),
    }
    if not any(direct_params.values()):
        return

    payload = {}
    if direct_params["animal_id"]:
        payload["id"] = direct_params["animal_id"]
    if direct_params["birth_date"]:
        payload["birth_date"] = direct_params["birth_date"]
    if direct_params["department"]:
        payload["department"] = direct_params["department"]
    if direct_params["check_items"]:
        payload["check_items"] = direct_params["check_items"]

    set_qr_form_defaults(parse_management_seal_qr(json.dumps(payload, ensure_ascii=False)))


def department_index(default_department=""):
    normalized = normalize_department_name(default_department)
    if normalized in DEPARTMENT_OPTIONS:
        return DEPARTMENT_OPTIONS.index(normalized)
    return 0


def format_check_items(check_items, other_text=""):
    labels = list(check_items or [])
    other_text = str(other_text or "").strip()
    if other_text:
        if "その他" in labels:
            labels = [label for label in labels if label != "その他"]
        labels.append(f"その他（{other_text}）")
    return ", ".join(labels)


def render_optional_report_fields(row):
    department = report_field(row, "部署")
    check_items = report_field(row, "チェック項目")
    if department:
        st.write(f"**部署:** {department}")
    if check_items:
        st.write(f"**チェック項目:** {check_items}")


def normalize_record_id(record_id):
    if record_id is None:
        return ""
    text = str(record_id).strip()
    if not text or text.lower() in ("nan", "none"):
        return ""
    try:
        return str(int(float(text)))
    except (ValueError, OverflowError):
        return text


def find_records_by_id(df, record_id):
    target_id = normalize_record_id(record_id)
    if not target_id or "記録ID" not in df.columns:
        return df.iloc[0:0]
    normalized_ids = df["記録ID"].apply(normalize_record_id)
    return df[normalized_ids == target_id]


def build_dashboard_url(record_id):
    app_url = get_app_url()
    if not app_url:
        return ""
    return f"{app_url}?view=dashboard&record_id={normalize_record_id(record_id)}"


def is_valid_line_user_id(user_id):
    return bool(re.fullmatch(r"U[a-f0-9]{32}", user_id, re.IGNORECASE))


def line_user_id_error_message(user_id):
    if is_valid_line_user_id(user_id):
        return ""
    if user_id.startswith("@"):
        return "LINE ID（@から始まる名前）ではなく、User ID（Uから始まる33文字）を入力してください。"
    return (
        "LINE User ID の形式が正しくありません。"
        "「U」から始まる33文字（例: U1234567890abcdef1234567890abcdef）を入力してください。"
        "LINEの表示名や yakulutooisi のようなIDとは別物です。"
    )


def format_report_message(report, dashboard_url=""):
    message = (
        "【動物遠隔診療】新しい現場報告があります\n\n"
        f"判定: {report['トリアージ判定']}\n"
        f"個体: {report['個体識別番号']}\n"
        f"生年月日: {report['報告者名']}\n"
        f"部署: {report_field(report, '部署') or '未入力'}\n"
        f"体温: {report['体温']} ℃\n"
        f"チェック項目: {report_field(report, 'チェック項目') or 'なし'}\n"
        f"症状: {report['主な症状'] or 'なし'}\n"
        f"報告日時: {report['日時']}"
    )
    if dashboard_url:
        message += f"\n\n▼ 状況確認\n{dashboard_url}"
    return message


def build_line_messages(report):
    dashboard_url = build_dashboard_url(report["記録ID"])
    summary = (
        f"【{report['トリアージ判定']}】\n"
        f"個体: {report['個体識別番号']}\n"
        f"部署: {report_field(report, '部署') or '未入力'}\n"
        f"体温: {report['体温']}℃\n"
        f"チェック: {report_field(report, 'チェック項目') or 'なし'}\n"
        f"症状: {report['主な症状'] or 'なし'}\n"
        f"日時: {report['日時']}\n\n"
        "下のボタンからアプリを開き、\n"
        "指示・コメントを記入できます。\n"
        "（LINEトークへの返信でも連絡可能です）"
    )

    messages = [{"type": "text", "text": summary}]

    if dashboard_url:
        messages.append(
            {
                "type": "template",
                "altText": f"報告対応: {report['個体識別番号']}",
                "template": {
                    "type": "buttons",
                    "text": "報告内容の確認・指示の記入はこちら",
                    "actions": [
                        {
                            "type": "uri",
                            "label": "報告を確認・対応する",
                            "uri": dashboard_url,
                        }
                    ],
                },
            }
        )

    return messages


def send_line_notification(user_id, report):
    line_config = st.secrets.get("line")
    if not line_config or not line_config.get("channel_access_token"):
        return False, "LINE設定（secrets.toml の [line] channel_access_token）がありません。"

    try:
        response = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {line_config['channel_access_token']}",
            },
            json={
                "to": user_id,
                "messages": build_line_messages(report),
            },
            timeout=10,
        )
        response.raise_for_status()
        return True, "LINEに通知を送信しました。"
    except requests.HTTPError as e:
        detail = e.response.text if e.response is not None else str(e)
        return False, f"LINE送信エラー: {detail}"
    except Exception as e:
        return False, f"LINE送信エラー: {e}"


def send_line_text(user_id, message):
    line_config = st.secrets.get("line")
    if not line_config or not line_config.get("channel_access_token"):
        return False, "LINE設定がありません。"

    try:
        response = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {line_config['channel_access_token']}",
            },
            json={
                "to": user_id,
                "messages": [{"type": "text", "text": message}],
            },
            timeout=10,
        )
        response.raise_for_status()
        return True, "LINEに送信しました。"
    except requests.HTTPError as e:
        detail = e.response.text if e.response is not None else str(e)
        return False, f"LINE送信エラー: {detail}"
    except Exception as e:
        return False, f"LINE送信エラー: {e}"


def notify_line_recipient(user_id, report, label):
    user_id = user_id.strip()
    if not user_id:
        return []

    user_id_error = line_user_id_error_message(user_id)
    if user_id_error:
        return [(label, False, user_id_error)]

    ok, detail = send_line_notification(user_id, report)
    return [(label, ok, detail)]


def notify_veterinarian(report):
    settings = load_notification_settings()
    results = []

    if settings.get("LINE通知") == "有効":
        results.extend(
            notify_line_recipient(settings.get("LINE_User_ID", ""), report, "獣医師")
        )

    if settings.get("現場_LINE通知") == "有効":
        farm_message = (
            f"【報告送信完了】\n"
            f"個体: {report['個体識別番号']}\n"
            f"判定: {report['トリアージ判定']}\n\n"
            "獣医師からの指示はこのLINEに届きます。"
        )
        farm_user_id = settings.get("現場_LINE_User_ID", "").strip()
        if not farm_user_id:
            results.append(("現場", False, "現場 LINE User IDが未登録です。"))
        else:
            farm_error = line_user_id_error_message(farm_user_id)
            if farm_error:
                results.append(("現場", False, farm_error))
            else:
                ok, detail = send_line_text(farm_user_id, farm_message)
                results.append(("現場", ok, detail))

    return results


def notify_field_of_vet_response(report, comment):
    settings = load_notification_settings()
    results = []

    if settings.get("現場_LINE通知") != "有効":
        return results

    farm_user_id = settings.get("現場_LINE_User_ID", "").strip()
    if not farm_user_id:
        return [("現場", False, "現場 LINE User IDが未登録です。")]

    user_id_error = line_user_id_error_message(farm_user_id)
    if user_id_error:
        return [("現場", False, user_id_error)]

    message = (
        "【獣医師からの指示】\n\n"
        f"個体: {report['個体識別番号']}\n"
        f"判定: {report['トリアージ判定']}\n"
        f"報告日時: {report['日時']}\n\n"
        f"指示・コメント:\n{comment or '（コメントなし）'}\n\n"
        "返信はこのLINEにメッセージを送ってください。"
    )
    ok, detail = send_line_text(farm_user_id, message)
    results.append(("現場", ok, detail))
    return results


# -------------------------------------------------------------------------
# 画像・動画をGoogleドライブにアップロードする汎用関数
# -------------------------------------------------------------------------
def upload_file_to_drive(file_obj):
    FOLDER_ID = "1_5WgaqG2hkVswPqsrHlthke5-j0H8rnF"

    creds_dict = dict(st.secrets["connections"]["gsheets"])
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=[
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    drive_service = build("drive", "v3", credentials=creds)

    file_bytes = file_obj.getvalue()
    if not file_bytes:
        raise ValueError("ファイルが空です。もう一度選択してください。")

    file_size_mb = len(file_bytes) / (1024 * 1024)
    if file_size_mb > 200:
        raise ValueError(f"ファイルが大きすぎます（{file_size_mb:.1f}MB）。200MB以下にしてください。")

    mime_type = file_obj.type or "application/octet-stream"
    prefix = "video" if "video" in mime_type else "photo"
    file_name = f"{prefix}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{file_obj.name}"

    file_metadata = {"name": file_name, "parents": [FOLDER_ID]}
    media = MediaIoBaseUpload(
        io.BytesIO(file_bytes),
        mimetype=mime_type,
        resumable=file_size_mb > 8,
    )

    try:
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        ).execute()
        file_id = file.get("id")

        drive_service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            supportsAllDrives=True,
        ).execute()
    except Exception as error:
        message = str(error)
        if "storageQuotaExceeded" in message or "Service Accounts do not have storage quota" in message:
            service_email = creds_dict.get("client_email", "サービスアカウント")
            raise RuntimeError(
                "Google Drive へのアップロード権限がありません。"
                f"アップロード先フォルダを {service_email} と「編集者」で共有してください。"
            ) from error
        raise

    return file_id


def format_media_ref(file_id, media_type):
    return f"{media_type}:{file_id}"


def parse_media_ref(file_data):
    text = str(file_data).strip()
    if not text or text in ["ファイルなし", "写真なし", "アップロード失敗"]:
        return None, None
    if text.startswith("video:"):
        return "video", text[6:]
    if text.startswith("photo:"):
        return "photo", text[6:]
    return "unknown", text


# -------------------------------------------------------------------------
# 問診記録の読み込み・表示
# -------------------------------------------------------------------------
def load_records_df(*, refresh=False):
    if refresh:
        st.session_state.pop(RECORDS_CACHE_KEY, None)
    if RECORDS_CACHE_KEY in st.session_state:
        return st.session_state[RECORDS_CACHE_KEY]

    df = conn.read(worksheet="問診記録", ttl=SHEET_READ_TTL)
    df = df.fillna("")
    if "記録ID" in df.columns:
        df["記録ID"] = df["記録ID"].apply(normalize_record_id)
    st.session_state[RECORDS_CACHE_KEY] = df
    return df


def normalize_animal_id(animal_id):
    text = str(animal_id).strip()
    if not text or text.lower() in ("nan", "none"):
        return ""
    try:
        number = float(text)
        if number.is_integer():
            return str(int(number))
    except ValueError:
        pass
    return text


def get_registered_animal_ids(df):
    ids = df["個体識別番号"].apply(normalize_animal_id)
    return sorted(ids[ids != ""].unique().tolist())


def filter_records_by_animal_id(df, animal_id):
    target = normalize_animal_id(animal_id)
    normalized = df["個体識別番号"].apply(normalize_animal_id)
    return df[normalized == target]


def filter_animal_ids(animal_ids, search_query):
    query = search_query.strip().lower()
    if not query:
        return animal_ids
    return [animal_id for animal_id in animal_ids if query in animal_id.lower()]


def sort_records_by_datetime(df):
    if df.empty:
        return df
    sorted_df = df.copy()
    sorted_df["_sort_dt"] = pd.to_datetime(sorted_df["日時"], errors="coerce")
    sorted_df = sorted_df.sort_values("_sort_dt", ascending=False).drop(columns="_sort_dt")
    return sorted_df


def render_media(file_data):
    text = str(file_data).strip()
    if text == "アップロード失敗":
        st.error("メディアのアップロードに失敗しました。報告フォームから再度送信してください。")
        return

    media_type, file_id = parse_media_ref(file_data)
    if not file_id:
        st.info("メディア添付なし")
        return

    preview_url = f"https://drive.google.com/file/d/{file_id}/preview"
    open_url = f"https://drive.google.com/file/d/{file_id}/view"

    if media_type in ("video", "unknown"):
        components.iframe(preview_url, height=360, scrolling=True)
        st.caption("現場からの動画")
    else:
        image_url = f"https://drive.google.com/uc?id={file_id}"
        try:
            st.image(image_url, caption="現場からの写真", use_container_width=True)
        except Exception:
            components.iframe(preview_url, height=360, scrolling=True)
            st.caption("現場からの写真")

    st.link_button("Google Driveで開く", open_url, use_container_width=True)


def render_report_detail(df, row_idx, row, *, editable=False):
    st.markdown(f"### 【{row['トリアージ判定']}】 個体: {row['個体識別番号']}")

    col1, col2 = st.columns([2, 1])

    with col1:
        st.write(f"**報告日時:** {row['日時']} | **牛の生年月日:** {row['報告者名']}")
        render_optional_report_fields(row)
        st.write(f"**体温:** {row['体温']} ℃")
        st.write(f"**症状:** {row['主な症状'] or 'なし'}")
        st.write(f"**確認ステータス:** {row['確認ステータス']}")
        if row["獣医師コメント"]:
            st.write(f"**獣医師コメント:** {row['獣医師コメント']}")

        if editable and row["確認ステータス"] == "未確認":
            comment = st.text_area(
                "指示・コメントを入力",
                placeholder="例: 経過観察を継続してください。明日も体温を測定してください。",
                key=f"comment_{row_idx}",
            )
            if st.button("対応完了にして現場へLINE通知", key=f"btn_{row_idx}", type="primary"):
                df.loc[row_idx, "確認ステータス"] = "対応完了"
                df.loc[row_idx, "獣医師コメント"] = comment
                conn.update(worksheet="問診記録", data=df)
                clear_sheet_cache(records=True, notifications=False)

                updated_row = df.loc[row_idx].to_dict()
                notification_results = notify_field_of_vet_response(updated_row, comment)
                if notification_results:
                    for channel, ok, detail in notification_results:
                        if ok:
                            st.success(f"{channel}通知: {detail}")
                        else:
                            st.warning(f"{channel}通知: {detail}")
                else:
                    st.caption("現場へのLINE通知は「通知設定」タブで有効化できます。")

                st.success("ステータスとコメントを更新しました。")
                st.rerun()
        elif editable and row["確認ステータス"] == "対応完了":
            st.info("この報告は対応済みです。")
        elif not editable:
            st.caption("詳細な指示の記入は、獣医師用ダッシュボードまたはLINE通知のリンクから行えます。")

    with col2:
        render_media(row["患部写真"])


def render_vet_dashboard(focus_record_id=None, df=None):
    if df is None:
        df = load_records_df()

    if focus_record_id:
        matches = find_records_by_id(df, focus_record_id)
        if matches.empty:
            st.error(
                f"指定された報告が見つかりません。"
                f"（記録ID: {normalize_record_id(focus_record_id)}）"
            )
            st.caption("テスト通知のリンクはスプレッドシートに記録がないため開けません。現場からの実際の報告でお試しください。")
            return

        row_idx = matches.index[0]
        row = matches.iloc[0]
        render_report_detail(df, row_idx, row, editable=True)
        return

    unconfirmed_df = df[df["確認ステータス"] == "未確認"]

    if unconfirmed_df.empty:
        st.info("現在、未対応の報告はありません。")
        return

    indices = unconfirmed_df.index.tolist()

    def format_option(idx):
        row = unconfirmed_df.loc[idx]
        return f"{row['日時']} - 個体: {row['個体識別番号']} (生年月日: {row['報告者名']})"

    selected_idx = st.selectbox("対応する報告を選択してください", indices, format_func=format_option)

    if selected_idx is not None:
        row = unconfirmed_df.loc[selected_idx]
        with st.container():
            render_report_detail(df, selected_idx, row, editable=True)


# LINE通知のURLから開いた場合は、該当報告のダッシュボードを直接表示
if get_query_param("view") == "dashboard":
    st.header("獣医師用ダッシュボード")
    focus_record_id = get_query_param("record_id")
    if focus_record_id:
        st.info(
            "LINE通知から開きました。"
            "報告内容を確認し、指示・コメントを記入して「対応完了にして現場へLINE通知」を押してください。"
        )
    try:
        render_vet_dashboard(focus_record_id=focus_record_id)
    except Exception as e:
        st.error(sheet_read_error_message(e))
    st.stop()


# 問診記録は1回だけ読み込み、各タブで使い回す
try:
    shared_records_df = load_records_df()
except Exception as e:
    shared_records_df = None
    shared_records_error = sheet_read_error_message(e)


# タブの作成
tab1, tab2, tab3, tab4 = st.tabs([
    "現場からの報告フォーム",
    "獣医師用ダッシュボード",
    "個体履歴照会",
    "通知設定",
])

# -------------------------------------------------------------------------
# タブ1: 現場からの報告
# -------------------------------------------------------------------------
with tab1:
    st.header("現場報告入力")
    st.caption(
        "現場が報告を登録すると、獣医師のLINEに通知が届きます。"
        "獣医師の指示はLINEで返信が届きます。"
    )

    apply_management_seal_query_params()
    qr_defaults = get_qr_form_defaults()

    with st.expander("管理シールQRコード", expanded=bool(qr_defaults)):
        qr_text = st.text_input(
            "QRコードの読み取り結果を貼り付け",
            placeholder="例: 12345,2024-01-15,ベビー室 または JSON / URL形式",
            key="management_seal_qr_text",
        )
        if st.button("QRの内容をフォームに反映", key="apply_management_seal_qr"):
            parsed = parse_management_seal_qr(qr_text)
            if not any([
                parsed.get("animal_id"),
                parsed.get("birth_date"),
                parsed.get("department"),
                parsed.get("check_items"),
            ]):
                st.warning("QRの内容を読み取れませんでした。読み取り結果をそのまま貼り付けてください。")
            else:
                set_qr_form_defaults(parsed)
                st.success("QRの内容をフォームに反映しました。")
                st.rerun()

        if qr_defaults:
            st.caption(
                "反映中: "
                f"個体={qr_defaults.get('animal_id') or '未設定'} / "
                f"部署={qr_defaults.get('department') or '未設定'}"
            )

    if shared_records_df is None:
        st.error(shared_records_error)
        registered_animal_ids = []
    else:
        registered_animal_ids = get_registered_animal_ids(shared_records_df)

    if registered_animal_ids:
        quick_pick = st.selectbox(
            "登録済みの個体から選ぶ（任意）",
            ["（新規入力）"] + registered_animal_ids,
            key="report_animal_quick_pick",
        )
        default_animal_id = "" if quick_pick == "（新規入力）" else quick_pick
    else:
        default_animal_id = ""

    if qr_defaults.get("animal_id"):
        default_animal_id = qr_defaults["animal_id"]

    default_birth_date = qr_defaults.get("birth_date") or (datetime.now() - timedelta(days=365)).date()
    default_department = normalize_department_name(qr_defaults.get("department", ""))
    default_check_items = normalize_check_items(qr_defaults.get("check_items", []))

    uploaded_file = st.file_uploader(
        "患部の写真または動画をアップロード",
        type=["jpg", "jpeg", "png", "mp4", "mov"],
        key="report_media_file",
    )
    if uploaded_file is not None:
        file_size_mb = len(uploaded_file.getvalue()) / (1024 * 1024)
        st.caption(f"選択中: {uploaded_file.name}（{uploaded_file.type or '不明'} / {file_size_mb:.1f}MB）")

    with st.form("report_form", clear_on_submit=False):
        animal_id = st.text_input("個体識別番号 / 名前", value=default_animal_id)
        birth_date = st.date_input(
            "牛の生年月日",
            value=default_birth_date,
            format="YYYY/MM/DD",
        )
        department = st.selectbox(
            "あなたの部署",
            DEPARTMENT_OPTIONS,
            index=department_index(default_department),
        )
        check_items = st.multiselect(
            "チェック項目（複数選択可）",
            CHECK_ITEM_OPTIONS,
            default=default_check_items,
        )
        check_other_text = ""
        if "その他" in check_items:
            check_other_text = st.text_input("チェック項目（その他の内容）")
        temperature = st.number_input("測定体温 (C)", min_value=30.0, max_value=45.0, value=38.5, step=0.1)

        symptoms = st.multiselect(
            "主な症状（複数選択可）",
            CHECK_ITEM_OPTIONS,
        )
        
        confirm_send = st.checkbox("すべての入力が完了しました（チェックを入れてから送信）")
        submit_button = st.form_submit_button("報告を送信する")
        
        if submit_button:
            if not confirm_send:
                st.warning("誤送信を防ぐため、「すべての入力が完了しました」にチェックを入れてから送信ボタンを押してください。")
            elif not animal_id:
                st.error("個体識別番号は必須です。")
            else:
                with st.spinner('データを送信しています...'):
                    triage = "低・定例報告"
                    if temperature >= 40.0 or "出血" in symptoms or "ぐったりしている" in symptoms:
                        triage = "高・即時相談"
                    elif temperature >= 39.3 or len(symptoms) > 0:
                        triage = "中・要観察"
                    
                    file_id = "ファイルなし"
                    media_file = uploaded_file or st.session_state.get("report_media_file")
                    if media_file is not None:
                        try:
                            media_type = "video" if "video" in (media_file.type or "") else "photo"
                            drive_file_id = upload_file_to_drive(media_file)
                            file_id = format_media_ref(drive_file_id, media_type)
                        except Exception as e:
                            st.error(f"ファイルのアップロードに失敗しました: {e}")
                            file_id = "アップロード失敗"

                    record_id = str(int(datetime.now(timezone(timedelta(hours=+9))).timestamp()))
                    now_jst = datetime.now(timezone(timedelta(hours=+9))).strftime("%Y-%m-%d %H:%M")
                    birth_date_str = birth_date.strftime("%Y-%m-%d")
                    
                    check_items_text = format_check_items(check_items, check_other_text)

                    new_row = {
                        "記録ID": record_id,
                        "日時": now_jst,
                        "報告者名": birth_date_str,
                        "個体識別番号": animal_id,
                        "部署": department,
                        "体温": temperature,
                        "チェック項目": check_items_text,
                        "主な症状": ", ".join(symptoms),
                        "患部写真": file_id,
                        "トリアージ判定": triage,
                        "確認ステータス": "未確認",
                        "獣医師コメント": "",
                    }
                    new_data = pd.DataFrame([new_row])
                    
                    try:
                        existing_data = load_records_df(refresh=True)
                        updated_data = pd.concat([existing_data, new_data], ignore_index=True)
                        conn.update(worksheet="問診記録", data=updated_data)
                        clear_sheet_cache(records=True, notifications=False)
                        st.session_state.pop(QR_DEFAULTS_SESSION_KEY, None)
                        st.success(f"スプレッドシートへの送信が完了しました。 判定結果: {triage}")

                        notification_results = notify_veterinarian(new_row)
                        if notification_results:
                            for channel, ok, detail in notification_results:
                                if ok:
                                    st.info(f"{channel}通知: {detail}")
                                else:
                                    st.warning(f"{channel}通知: {detail}")
                        else:
                            st.caption("獣医師へのLINE通知は「通知設定」タブで有効化できます。")
                    except Exception as e:
                        st.error(f"送信エラーが発生しました: {e}")

# -------------------------------------------------------------------------
# タブ2: 獣医師用ダッシュボード
# -------------------------------------------------------------------------
with tab2:
    st.header("報告内容の確認・対応")
    st.caption(
        "未対応の報告を選び、指示・コメントを記入して送信すると現場のLINEに届きます。"
        "自由なやり取りは公式LINEのトークでも可能です。"
    )

    if shared_records_df is None:
        st.error(shared_records_error)
    else:
        render_vet_dashboard(df=shared_records_df)

# -------------------------------------------------------------------------
# タブ3: 個体履歴照会
# -------------------------------------------------------------------------
with tab3:
    st.header("個体別 過去記録")
    st.caption("登録済みの個体番号を選ぶか、検索して過去の報告を確認できます。")

    if shared_records_df is None:
        st.error(shared_records_error)
    else:
        history_df = shared_records_df
        registered_ids = get_registered_animal_ids(history_df)

        if not registered_ids:
            st.info("まだ登録されている個体がありません。現場から報告を送信すると、ここに履歴が表示されます。")
        else:
            search_query = st.text_input(
                "個体番号で検索",
                placeholder="例: A-001 や 名前の一部",
                key="history_search",
            )
            filtered_ids = filter_animal_ids(registered_ids, search_query)

            if not filtered_ids:
                st.warning("検索条件に一致する個体がありません。")
            else:
                selected_animal_id = st.selectbox(
                    "個体を選択",
                    filtered_ids,
                    format_func=lambda animal_id: (
                        f"{animal_id}（{len(filter_records_by_animal_id(history_df, animal_id))}件）"
                    ),
                    key="history_animal_select",
                )

                animal_records = sort_records_by_datetime(
                    filter_records_by_animal_id(history_df, selected_animal_id)
                )

                if animal_records.empty:
                    st.warning("選択した個体の記録が見つかりません。")
                else:
                    latest_record = animal_records.iloc[0]

                    col1, col2, col3 = st.columns(3)
                    col1.metric("報告回数", f"{len(animal_records)} 件")
                    col2.metric("最新報告", latest_record["日時"])
                    col3.metric("生年月日", latest_record["報告者名"])

                    st.divider()
                    st.subheader("報告一覧")

                    summary_columns = [
                        "日時", "部署", "体温", "チェック項目", "主な症状",
                        "トリアージ判定", "確認ステータス", "獣医師コメント",
                    ]
                    available_columns = [
                        column for column in summary_columns if column in animal_records.columns
                    ]
                    summary_df = animal_records[available_columns].copy()
                    st.dataframe(summary_df, use_container_width=True, hide_index=True)

                    record_options = animal_records.index.tolist()

                    def format_history_option(idx):
                        row = animal_records.loc[idx]
                        return f"{row['日時']} — {row['トリアージ判定']}（{row['確認ステータス']}）"

                    selected_record_idx = st.selectbox(
                        "詳細を見る報告を選択",
                        record_options,
                        format_func=format_history_option,
                        key="history_record_select",
                    )

                    if selected_record_idx is not None:
                        detail_row = animal_records.loc[selected_record_idx]
                        st.markdown(f"### 【{detail_row['トリアージ判定']}】 {selected_animal_id}")

                        detail_col1, detail_col2 = st.columns([2, 1])
                        with detail_col1:
                            st.write(f"**報告日時:** {detail_row['日時']}")
                            st.write(f"**牛の生年月日:** {detail_row['報告者名']}")
                            render_optional_report_fields(detail_row)
                            st.write(f"**体温:** {detail_row['体温']} ℃")
                            st.write(f"**症状:** {detail_row['主な症状'] or 'なし'}")
                            st.write(f"**確認ステータス:** {detail_row['確認ステータス']}")
                            if detail_row["獣医師コメント"]:
                                st.write(f"**獣医師コメント:** {detail_row['獣医師コメント']}")
                        with detail_col2:
                            render_media(detail_row["患部写真"])

# -------------------------------------------------------------------------
# タブ4: 通知設定
# -------------------------------------------------------------------------
with tab4:
    st.header("LINE連絡設定")
    st.caption(
        "現場→獣医師（報告通知）・獣医師→現場（対応完了通知）の双方向通知を設定します。"
    )

    current_settings = load_notification_settings()

    with st.form("notification_settings_form"):
        st.subheader("獣医師（通知を受け取る人）")
        line_user_id = st.text_input(
            "獣医師 LINE User ID",
            value=current_settings.get("LINE_User_ID", ""),
            placeholder="U1234567890abcdef1234567890abcdef",
        )
        line_enabled = st.checkbox(
            "獣医師への報告通知を有効にする",
            value=current_settings.get("LINE通知") == "有効",
        )

        st.divider()
        st.subheader("現場担当者（返信を受け取る人）")
        farm_line_user_id = st.text_input(
            "現場 LINE User ID",
            value=current_settings.get("現場_LINE_User_ID", ""),
            placeholder="U1234567890abcdef1234567890abcdef",
        )
        farm_line_enabled = st.checkbox(
            "現場への受付確認・返信通知を有効にする",
            value=current_settings.get("現場_LINE通知") == "有効",
        )

        save_settings = st.form_submit_button("設定を保存")

    if save_settings:
        new_settings = {
            "LINE_User_ID": line_user_id.strip(),
            "LINE通知": "有効" if line_enabled else "無効",
            "現場_LINE_User_ID": farm_line_user_id.strip(),
            "現場_LINE通知": "有効" if farm_line_enabled else "無効",
        }
        try:
            save_notification_settings(new_settings)
            st.success("通知設定を保存しました。")
        except Exception as e:
            st.error(
                f"設定の保存に失敗しました: {e}\n\n"
                "Googleスプレッドシートに「通知設定」シート（列: キー, 値）を作成してください。"
            )

    st.divider()
    st.subheader("運用フロー")
    st.markdown(
        "**1. 現場（アプリ）** → 報告フォームで登録 → 獣医師のLINEに通知\n\n"
        "**2. 獣医師（LINE → アプリ）** → 通知の「報告を確認・対応する」をタップ "
        "→ アプリで指示・コメントを記入 → 現場のLINEに通知\n\n"
        "**3. 自由なやり取り（LINE）** → 獣医師・現場が公式LINEにメッセージを送ると、"
        "相手に転送されます（下記 Webhook 設定が必要）"
    )

    st.subheader("LINEトーク連携の設定（任意）")
    st.markdown(
        "1. **獣医師・現場担当者** が公式LINEを友だち追加し、User ID を上に登録\n"
        "2. [LINE Official Account Manager](https://manager.line.biz/) → **設定 → 応答設定**\n"
        "   - 自動応答の「個別のお問い合わせを受け付けておりません」を **OFF**\n"
        "   - **Webhook** を ON\n"
        "3. `line_webhook.gs` を Google Apps Script にデプロイし、Webhook URL を LINE Developers に設定\n"
        "4. 以降、獣医師 ↔ 現場 のメッセージが公式LINE経由で **相互に転送** されます"
    )

    st.divider()
    st.subheader("テスト通知")
    if st.button("テスト通知を送信"):
        test_report = {
            "記録ID": "test-notification",
            "トリアージ判定": "中・要観察",
            "個体識別番号": "テスト個体",
            "報告者名": "2020-01-01",
            "体温": 39.0,
            "主な症状": "テスト通知",
            "日時": datetime.now(timezone(timedelta(hours=+9))).strftime("%Y-%m-%d %H:%M"),
        }
        results = notify_veterinarian(test_report)
        if not results:
            st.warning("LINE通知が無効です。上で「LINE通知を有効にする」にチェックしてください。")
        else:
            for channel, ok, detail in results:
                if ok:
                    st.success(f"{channel}: {detail}")
                else:
                    st.error(f"{channel}: {detail}")

    with st.expander("LINE Messaging API のサーバー設定（管理者向け）"):
        st.code(
            """# .streamlit/secrets.toml に追加

[line]
channel_access_token = "YOUR_CHANNEL_ACCESS_TOKEN"
app_url = "https://your-app.streamlit.app"
""",
            language="toml",
        )
        st.write("通知の「状況を確認」ボタンは `app_url?view=dashboard&record_id=...` を開きます。")
        st.markdown(
            "**User ID の確認方法（初回のみ）**\n"
            "1. [webhook.site](https://webhook.site/) で一時URLを取得\n"
            "2. LINE Developers → Messaging API → Webhook URL にそのURLを設定し「Use webhook」をON\n"
            "3. 公式LINEに友だち追加してメッセージを送信\n"
            "4. webhook.site に表示された JSON の `source.userId` をコピー"
        )
        st.write("Googleスプレッドシートの「通知設定」シート:")
        st.markdown("| キー | 値 |")
        st.markdown("| --- | --- |")
        for key in NOTIFICATION_SETTINGS_KEYS:
            st.markdown(f"| {key} | （アプリから自動保存） |")
