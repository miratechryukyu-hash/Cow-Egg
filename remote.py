import streamlit as st
import pandas as pd
from datetime import datetime
import os

st.set_page_config(page_title="動物遠隔診療MVP", layout="wide")
st.title("動物遠隔診療サポートシステム")

# プロトタイプ用の簡易データ保存
if "records" not in st.session_state:
    st.session_state.records = []

# タブの作成（現場用と獣医用）
tab1, tab2 = st.tabs(["現場からの報告フォーム", "獣医師用ダッシュボード"])

# -------------------------------------------------------------------------
# タブ1: 現場からの報告
# -------------------------------------------------------------------------
with tab1:
    st.header("現場報告入力")
    
    with st.form("report_form", clear_on_submit=True):
        reporter = st.text_input("報告者名")
        animal_id = st.text_input("個体識別番号 / 名前")
        temperature = st.number_input("測定体温 (C)", min_value=30.0, max_value=45.0, value=38.5, step=0.1)
        
        symptoms = st.multiselect(
            "主な症状（複数選択可）",
            ["食欲不振", "歩行異常", "出血", "下痢・嘔吐", "ぐったりしている", "その他"]
        )
        
        # スマホからアクセスするとカメラが起動します
        uploaded_file = st.file_uploader("患部の写真を撮影・アップロード", type=["jpg", "jpeg", "png"])
        
        submit_button = st.form_submit_with_button("報告を送信する")
        
        if submit_button:
            if not reporter or not animal_id:
                st.error("報告者名と個体識別番号は必須です。")
            else:
                # ルールベースのトリアージ判定
                triage = "低・定例報告"
                if temperature >= 40.0 or "出血" in symptoms or "ぐったりしている" in symptoms:
                    triage = "高・即時相談"
                elif temperature >= 39.3 or len(symptoms) > 0:
                    triage = "中・要観察"
                
                # レコードの作成
                new_record = {
                    "id": len(st.session_state.records) + 1,
                    "datetime": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "reporter": reporter,
                    "animal_id": animal_id,
                    "temperature": temperature,
                    "symptoms": ", ".join(symptoms),
                    "triage": triage,
                    "status": "未確認",
                    "comment": "",
                    "image": uploaded_file.read() if uploaded_file else None
                }
                
                st.session_state.records.append(new_record)
                
                # 画面表示
                st.success(f"送信完了しました。 判定結果: {triage}")
                if "高" in triage:
                    st.warning("警告: 緊急度が高いです。必要に応じて直接電話連絡も行ってください。")

# -------------------------------------------------------------------------
# タブ2: 獣医師用ダッシュボード
# -------------------------------------------------------------------------
with tab2:
    st.header("未対応の報告一覧")
    
    if not st.session_state.records:
        st.info("現在、未対応の報告はありません。")
    else:
        # 未確認のものを取り出して表示
        df = pd.DataFrame(st.session_state.records)
        
        for idx, row in df.iterrows():
            with st.container():
                st.markdown(f"### 【{row['triage']}】 個体: {row['animal_id']} (ステータス: {row['status']})")
                col1, col2 = st.columns([2, 1])
                
                with col1:
                    st.write(f"**報告日時:** {row['datetime']} | **報告者:** {row['reporter']}")
                    st.write(f"**体温:** {row['temperature']} C")
                    st.write(f"**症状:** {row['symptoms']}")
                    
                    # 獣医師のコメント入力
                    comment = st.text_area(f"指示・コメントを入力 ({row['animal_id']})", key=f"com_{row['id']}")
                    if st.button(f"対応完了にする ({row['animal_id']})", key=f"btn_{row['id']}"):
                        st.session_state.records[idx]['status'] = "対応完了"
                        st.session_state.records[idx]['comment'] = comment
                        st.success("ステータスを更新しました。")
                        st.rerun()
                
                with col2:
                    if row['image']:
                        st.image(row['image'], caption="現場からの写真", use_container_width=True)
                    else:
                        st.text("写真なし")
                st.markdown("---")
