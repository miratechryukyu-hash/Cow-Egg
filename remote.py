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
import os
import re

# カレンダーのUIを強制的に日本語（日本地域）にする設定
os.environ["LC_ALL"] = "ja_JP.UTF-8"
os.environ["LANG"] = "ja_JP.UTF-8"

st.set_page_config(page_title="動物遠隔診療MVP", layout="wide")

st.title("遠隔診療システム")

# Googleスプレッドシートへの接続設定
conn = st.connection("gsheets", type=GSheetsConnection)

NOTIFICATION_SETTINGS_KEYS = [
    "LINE_User_ID",
    "LINE通知",
]

DEFAULT_NOTIFICATION_SETTINGS = {
    "LINE_User_ID": "",
    "LINE通知": "無効",
}

# -------------------------------------------------------------------------
# 通知設定の読み書き
# -------------------------------------------------------------------------
def load_notification_settings():
    try:
        df = conn.read(worksheet="通知設定", ttl=0)
        df = df.fillna("")
        settings = DEFAULT_NOTIFICATION_SETTINGS.copy()
        for _, row in df.iterrows():
            key = str(row.get("キー", "")).strip()
            if key in settings:
                settings[key] = str(row.get("値", "")).strip()
        return settings
    except Exception:
        return DEFAULT_NOTIFICATION_SETTINGS.copy()


def save_notification_settings(settings):
    rows = [{"キー": key, "値": settings.get(key, "")} for key in NOTIFICATION_SETTINGS_KEYS]
    df = pd.DataFrame(rows)
    conn.update(worksheet="通知設定", data=df)


def get_app_url():
    line_config = st.secrets.get("line", {})
    app_config = st.secrets.get("app", {})
    return (line_config.get("app_url") or app_config.get("url") or "").rstrip("/")


def get_query_param(name):
    value = st.query_params.get(name)
    if isinstance(value, list):
        return value[0] if value else None
    return value


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
        f"体温: {report['体温']} ℃\n"
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
        f"体温: {report['体温']}℃\n"
        f"症状: {report['主な症状'] or 'なし'}\n"
        f"日時: {report['日時']}"
    )

    if dashboard_url:
        return [
            {
                "type": "template",
                "altText": f"新しい現場報告: {report['個体識別番号']}",
                "template": {
                    "type": "buttons",
                    "text": summary[:160],
                    "actions": [
                        {
                            "type": "uri",
                            "label": "状況を確認",
                            "uri": dashboard_url,
                        }
                    ],
                },
            }
        ]

    return [{"type": "text", "text": format_report_message(report)}]


def send_line_notification(user_id, report):
    line_config = st.secrets.get("line")
    if not line_config or not line_config.get("channel_access_token"):
        return False, "LINE設定（secrets.toml の [line] channel_access_token）がありません。"

    if not get_app_url():
        return False, "アプリURL（secrets.toml の [line] app_url）がありません。"

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


def notify_veterinarian(report):
    settings = load_notification_settings()
    results = []

    if settings.get("LINE通知") != "有効":
        return results

    user_id = settings.get("LINE_User_ID", "").strip()
    if not user_id:
        results.append(("LINE", False, "LINE User IDが未登録です。"))
        return results

    user_id_error = line_user_id_error_message(user_id)
    if user_id_error:
        results.append(("LINE", False, user_id_error))
        return results

    ok, detail = send_line_notification(user_id, report)
    results.append(("LINE", ok, detail))
    return results


# -------------------------------------------------------------------------
# 画像・動画をGoogleドライブにアップロードする汎用関数
# -------------------------------------------------------------------------
def upload_file_to_drive(file_obj):
    FOLDER_ID = '1_5WgaqG2hkVswPqsrHlthke5-j0H8rnF'
    
    creds_dict = dict(st.secrets["connections"]["gsheets"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/drive"])
    drive_service = build('drive', 'v3', credentials=creds)
    
    prefix = "video" if "video" in file_obj.type else "photo"
    file_name = f"{prefix}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{file_obj.name}"
    
    file_metadata = {'name': file_name, 'parents': [FOLDER_ID]}
    media = MediaIoBaseUpload(io.BytesIO(file_obj.getvalue()), mimetype=file_obj.type, resumable=True)
    
    file = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    file_id = file.get('id')
    
    drive_service.permissions().create(fileId=file_id, body={'type': 'anyone', 'role': 'reader'}).execute()
    
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
def load_records_df():
    df = conn.read(worksheet="問診記録", ttl=0)
    df = df.fillna("")
    if "記録ID" in df.columns:
        df["記録ID"] = df["記録ID"].apply(normalize_record_id)
    return df


def get_registered_animal_ids(df):
    ids = df["個体識別番号"].astype(str).str.strip()
    return sorted(ids[ids != ""].unique().tolist())


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


def render_report_detail(df, row_idx, row, *, allow_complete=True):
    st.markdown(f"### 【{row['トリアージ判定']}】 個体: {row['個体識別番号']}")

    col1, col2 = st.columns([2, 1])

    with col1:
        st.write(f"**報告日時:** {row['日時']} | **牛の生年月日:** {row['報告者名']}")
        st.write(f"**体温:** {row['体温']} ℃")
        st.write(f"**症状:** {row['主な症状'] or 'なし'}")
        st.write(f"**確認ステータス:** {row['確認ステータス']}")
        if row["獣医師コメント"]:
            st.write(f"**獣医師コメント:** {row['獣医師コメント']}")

        if allow_complete and row["確認ステータス"] == "未確認":
            comment = st.text_area("指示・コメントを入力", key=f"comment_{row_idx}")
            if st.button("対応完了にする", key=f"btn_{row_idx}"):
                df.loc[row_idx, "確認ステータス"] = "対応完了"
                df.loc[row_idx, "獣医師コメント"] = comment
                conn.update(worksheet="問診記録", data=df)
                st.success("ステータスとコメントを更新しました。")
                st.rerun()
        elif row["確認ステータス"] == "対応完了":
            st.info("この報告は対応済みです。")

    with col2:
        render_media(row["患部写真"])


def render_vet_dashboard(focus_record_id=None):
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
        render_report_detail(df, row_idx, row)
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
            render_report_detail(df, selected_idx, row)


# LINE通知のURLから開いた場合は、該当報告のダッシュボードを直接表示
if get_query_param("view") == "dashboard":
    st.header("獣医師用ダッシュボード")
    focus_record_id = get_query_param("record_id")
    if focus_record_id:
        st.info("LINE通知から開きました。該当の報告を表示しています。")
    try:
        render_vet_dashboard(focus_record_id=focus_record_id)
    except Exception as e:
        st.error(f"データ読み込みエラー: {e}")
    st.stop()


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

    try:
        all_records_df = load_records_df()
        registered_animal_ids = get_registered_animal_ids(all_records_df)
    except Exception:
        registered_animal_ids = []

    if registered_animal_ids:
        quick_pick = st.selectbox(
            "登録済みの個体から選ぶ（任意）",
            ["（新規入力）"] + registered_animal_ids,
            key="report_animal_quick_pick",
        )
        default_animal_id = "" if quick_pick == "（新規入力）" else quick_pick
    else:
        default_animal_id = ""

    with st.form("report_form", clear_on_submit=False):
        animal_id = st.text_input("個体識別番号 / 名前", value=default_animal_id)
        birth_date = st.date_input(
            "牛の生年月日", 
            value=datetime.now() - timedelta(days=365),
            format="YYYY/MM/DD"
        )
        temperature = st.number_input("測定体温 (C)", min_value=30.0, max_value=45.0, value=38.5, step=0.1)
        
        symptoms = st.multiselect(
            "主な症状（複数選択可）",
            ["食欲不振", "歩行異常", "出血", "下痢・嘔吐", "ぐったりしている", "その他"]
        )
        
        uploaded_file = st.file_uploader("患部の写真または動画をアップロード", type=["jpg", "jpeg", "png", "mp4", "mov"])
        
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
                    if uploaded_file is not None:
                        try:
                            media_type = "video" if "video" in uploaded_file.type else "photo"
                            drive_file_id = upload_file_to_drive(uploaded_file)
                            file_id = format_media_ref(drive_file_id, media_type)
                        except Exception as e:
                            st.error(f"ファイルのアップロードに失敗しました: {e}")
                            file_id = "アップロード失敗"

                    record_id = str(int(datetime.now(timezone(timedelta(hours=+9))).timestamp()))
                    now_jst = datetime.now(timezone(timedelta(hours=+9))).strftime("%Y-%m-%d %H:%M")
                    birth_date_str = birth_date.strftime("%Y-%m-%d")
                    
                    new_row = {
                        "記録ID": record_id,
                        "日時": now_jst,
                        "報告者名": birth_date_str,
                        "個体識別番号": animal_id,
                        "体温": temperature,
                        "主な症状": ", ".join(symptoms),
                        "患部写真": file_id, 
                        "トリアージ判定": triage,
                        "確認ステータス": "未確認",
                        "獣医師コメント": ""
                    }
                    new_data = pd.DataFrame([new_row])
                    
                    try:
                        existing_data = load_records_df()
                        updated_data = pd.concat([existing_data, new_data], ignore_index=True)
                        conn.update(worksheet="問診記録", data=updated_data)
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
    st.header("未対応の報告一覧")

    try:
        render_vet_dashboard()
    except Exception as e:
        st.error(f"データ読み込みエラー: {e}")

# -------------------------------------------------------------------------
# タブ3: 個体履歴照会
# -------------------------------------------------------------------------
with tab3:
    st.header("個体別 過去記録")
    st.caption("登録済みの個体番号を選ぶか、検索して過去の報告を確認できます。")

    try:
        history_df = load_records_df()
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
                        f"{animal_id}（{len(history_df[history_df['個体識別番号'] == animal_id])}件）"
                    ),
                    key="history_animal_select",
                )

                animal_records = sort_records_by_datetime(
                    history_df[history_df["個体識別番号"] == selected_animal_id]
                )
                latest_record = animal_records.iloc[0]

                col1, col2, col3 = st.columns(3)
                col1.metric("報告回数", f"{len(animal_records)} 件")
                col2.metric("最新報告", latest_record["日時"])
                col3.metric("生年月日", latest_record["報告者名"])

                st.divider()
                st.subheader("報告一覧")

                summary_df = animal_records[[
                    "日時", "体温", "主な症状", "トリアージ判定", "確認ステータス", "獣医師コメント"
                ]].copy()
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
                        st.write(f"**体温:** {detail_row['体温']} ℃")
                        st.write(f"**症状:** {detail_row['主な症状'] or 'なし'}")
                        st.write(f"**確認ステータス:** {detail_row['確認ステータス']}")
                        if detail_row["獣医師コメント"]:
                            st.write(f"**獣医師コメント:** {detail_row['獣医師コメント']}")
                    with detail_col2:
                        render_media(detail_row["患部写真"])

    except Exception as e:
        st.error(f"データ読み込みエラー: {e}")

# -------------------------------------------------------------------------
# タブ4: 通知設定
# -------------------------------------------------------------------------
with tab4:
    st.header("獣医師へのLINE通知設定")
    st.caption("現場から報告が送信されたとき、獣医師のLINEへプッシュ通知します。")

    current_settings = load_notification_settings()

    with st.form("notification_settings_form"):
        st.subheader("LINE通知")
        st.markdown(
            "1. [LINE Developers](https://developers.line.biz/) で Messaging API チャネル（公式アカウント）を作成\n"
            "2. **Channel access token** を管理者が Secrets に設定\n"
            "3. 獣医師が公式LINEアカウントを**友だち追加**し、任意のメッセージを送信\n"
            "4. Webhook ログ等で確認した **User ID（Uから始まる33文字）** を下に入力"
        )
        st.warning(
            "「yakulutooisi」のような LINE ID や表示名では通知できません。"
            "必ず `U` から始まる User ID を入力してください。"
        )
        line_user_id = st.text_input(
            "LINE User ID",
            value=current_settings.get("LINE_User_ID", ""),
            placeholder="U1234567890abcdef1234567890abcdef",
            help="Webhook ログの source.userId をコピーしてください。",
        )
        line_enabled = st.checkbox(
            "LINE通知を有効にする",
            value=current_settings.get("LINE通知") == "有効",
        )

        save_settings = st.form_submit_button("設定を保存")

    if save_settings:
        new_settings = {
            "LINE_User_ID": line_user_id.strip(),
            "LINE通知": "有効" if line_enabled else "無効",
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
