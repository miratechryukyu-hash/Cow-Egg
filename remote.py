import streamlit as st
import pandas as pd
from datetime import datetime
from streamlit_gsheets import GSheetsConnection
import io
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

st.set_page_config(page_title="動物遠隔診療MVP", layout="wide")
st.title("動物遠隔診療サポートシステム")

# Googleスプレッドシートへの接続設定
conn = st.connection("gsheets", type=GSheetsConnection)

# -------------------------------------------------------------------------
# 画像をGoogleドライブにアップロードする関数
# -------------------------------------------------------------------------
def upload_image_to_drive(file_obj):
    # ★ここに先ほどメモしたフォルダIDを貼り付けます（シングルクォーテーションで囲む）
    FOLDER_ID = '1_5WgaqG2hkVswPqsrHlthke5-j0H8rnF'
    
    # st.secretsから認証情報を読み込んでDrive APIに接続
    creds_dict = dict(st.secrets["connections"]["gsheets"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/drive"])
    drive_service = build('drive', 'v3', credentials=creds)
    
    # ファイルのアップロード準備
    file_metadata = {'name': f"photo_{datetime.now().strftime('%Y%m%d%H%M%S')}_{file_obj.name}", 'parents': [FOLDER_ID]}
    media = MediaIoBaseUpload(io.BytesIO(file_obj.getvalue()), mimetype=file_obj.type, resumable=True)
    
    # ドライブへ送信
    file = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    file_id = file.get('id')
    
    # アプリ上で画像を表示できるように権限を「リンクを知っている全員が閲覧可」に設定
    drive_service.permissions().create(fileId=file_id, body={'type': 'anyone', 'role': 'reader'}).execute()
    
    return file_id

# タブの作成
tab1, tab2 = st.tabs(["現場からの報告フォーム", "獣医師用ダッシュボード"])

# -------------------------------------------------------------------------
# タブ1: 現場からの報告
# -------------------------------------------------------------------------
with tab1:
    st.header("現場報告入力")
    
    with st.form("report_form", clear_on_submit=False):
        reporter = st.text_input("報告者名")
        animal_id = st.text_input("個体識別番号 / 名前")
        temperature = st.number_input("測定体温 (C)", min_value=30.0, max_value=45.0, value=38.5, step=0.1)
        
        symptoms = st.multiselect(
            "主な症状（複数選択可）",
            ["食欲不振", "歩行異常", "出血", "下痢・嘔吐", "ぐったりしている", "その他"]
        )
        
        # 【追加】写真のアップロード機能
        uploaded_file = st.file_uploader("患部の写真を撮影・アップロード", type=["jpg", "jpeg", "png"])
        
        confirm_send = st.checkbox("すべての入力が完了しました（チェックを入れてから送信）")
        submit_button = st.form_submit_button("報告を送信する")
        
        if submit_button:
            if not confirm_send:
                st.warning("誤送信を防ぐため、「すべての入力が完了しました」にチェックを入れてから送信ボタンを押してください。")
            elif not reporter or not animal_id:
                st.error("報告者名と個体識別番号は必須です。")
            else:
                with st.spinner('データを送信しています...'):
                    triage = "低・定例報告"
                    if temperature >= 40.0 or "出血" in symptoms or "ぐったりしている" in symptoms:
                        triage = "高・即時相談"
                    elif temperature >= 39.3 or len(symptoms) > 0:
                        triage = "中・要観察"
                    
                    # 写真があればGoogleドライブにアップロードしてIDを取得
                    photo_id = "写真なし"
                    if uploaded_file is not None:
                        try:
                            photo_id = upload_image_to_drive(uploaded_file)
                        except Exception as e:
                            st.error(f"写真のアップロードに失敗しました: {e}")
                            photo_id = "アップロード失敗"
                    
                    new_data = pd.DataFrame([{
                        "記録ID": str(int(datetime.now().timestamp())),
                        "日時": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "報告者名": reporter,
                        "個体識別番号": animal_id,
                        "体温": temperature,
                        "主な症状": ", ".join(symptoms),
                        "患部写真": photo_id, # ここに取得したID（または「写真なし」）が入ります
                        "トリアージ判定": triage,
                        "確認ステータス": "未確認",
                        "獣医師コメント": ""
                    }])
                    
                    try:
                        existing_data = conn.read(worksheet="問診記録", ttl=0)
                        updated_data = pd.concat([existing_data, new_data], ignore_index=True)
                        conn.update(worksheet="問診記録", data=updated_data)
                        st.success(f"スプレッドシートへの送信が完了しました。 判定結果: {triage}")
                    except Exception as e:
                        st.error(f"送信エラーが発生しました: {e}")

# -------------------------------------------------------------------------
# タブ2: 獣医師用ダッシュボード
# -------------------------------------------------------------------------
with tab2:
    st.header("未対応の報告一覧")
    
    try:
        df = conn.read(worksheet="問診記録", ttl=0)
        df = df.fillna("")
        
        unconfirmed_df = df[df["確認ステータス"] == "未確認"]
        
        if unconfirmed_df.empty:
            st.info("現在、未対応の報告はありません。")
        else:
            indices = unconfirmed_df.index.tolist()
            
            def format_option(idx):
                row = unconfirmed_df.loc[idx]
                return f"{row['日時']} - 個体: {row['個体識別番号']} (報告者: {row['報告者名']})"
            
            selected_idx = st.selectbox("対応する報告を選択してください", indices, format_func=format_option)
            
            if selected_idx is not None:
                row = unconfirmed_df.loc[selected_idx]
                
                with st.container():
                    st.markdown(f"### 【{row['トリアージ判定']}】 個体: {row['個体識別番号']}")
                    
                    col1, col2 = st.columns([2, 1])
                    
                    with col1:
                        st.write(f"**報告日時:** {row['日時']} | **報告者:** {row['報告者名']}")
                        st.write(f"**体温:** {row['体温']} C")
                        st.write(f"**症状:** {row['主な症状']}")
                        
                        comment = st.text_area("指示・コメントを入力", key=f"comment_{selected_idx}")
                        if st.button("対応完了にする", key=f"btn_{selected_idx}"):
                            df.loc[selected_idx, "確認ステータス"] = "対応完了"
                            df.loc[selected_idx, "獣医師コメント"] = comment
                            conn.update(worksheet="問診記録", data=df)
                            st.success("ステータスとコメントを更新しました。")
                            st.rerun()
                            
                    # 【追加】写真の表示エリア
                    with col2:
                        photo_data = row['患部写真']
                        if photo_data and photo_data != "写真なし" and photo_data != "アップロード失敗":
                            # Googleドライブの画像IDを使って画像を表示
                            image_url = f"https://drive.google.com/uc?id={photo_data}"
                            st.image(image_url, caption="現場からの写真", use_container_width=True)
                        else:
                            st.info("写真なし")
                            
    except Exception as e:
        st.error(f"データ読み込みエラー: {e}")
