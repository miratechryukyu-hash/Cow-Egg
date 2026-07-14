import streamlit as st
import pandas as pd
from datetime import datetime
from streamlit_gsheets import GSheetsConnection

st.set_page_config(page_title="動物遠隔診療MVP", layout="wide")
st.title("動物遠隔診療サポートシステム")

# Googleスプレッドシートへの接続設定
conn = st.connection("gsheets", type=GSheetsConnection)

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
        
        # 簡易運用のための写真なし版（まずは文字データ連携を確実に動かします）
        submit_button = st.form_submit_button("報告を送信する")
        
        if submit_button:
            if not reporter or not animal_id:
                st.error("報告者名と個体識別番号は必須です。")
            else:
                # トリアージ判定
                triage = "低・定例報告"
                if temperature >= 40.0 or "出血" in symptoms or "ぐったりしている" in symptoms:
                    triage = "高・即時相談"
                elif temperature >= 39.3 or len(symptoms) > 0:
                    triage = "中・要観察"
                
                # 新しいレコードのデータ
                new_data = pd.DataFrame([{
                    "記録ID": str(int(datetime.now().timestamp())),
                    "日時": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "報告者名": reporter,
                    "個体識別番号": animal_id,
                    "体温": temperature,
                    "主な症状": ", ".join(symptoms),
                    "患部写真": "写真なし",
                    "トリアージ判定": triage,
                    "確認ステータス": "未確認",
                    "獣医師コメント": ""
                }])
                
                try:
                    # 既存のデータを取得して結合（ここで問診記録のシートを探します）
                    existing_data = conn.read(worksheet="問診記録", ttl=0)
                    updated_data = pd.concat([existing_data, new_data], ignore_index=True)
                    
                    # スプレッドシートを更新
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
        # スプレッドシートから最新データを読み込み
        df = conn.read(worksheet="問診記録", ttl=0)
        
        # 未確認のデータのみ抽出
        unconfirmed_df = df[df["確認ステータス"] == "未確認"]
        
        if unconfirmed_df.empty:
            st.info("現在、未対応の報告はありません。")
        else:
            for idx, row in unconfirmed_df.iterrows():
                with st.container():
                    st.markdown(f"### 【{row['トリアージ判定']}】 個体: {row['個体識別番号']}")
                    st.write(f"**報告日時:** {row['日時']} | **報告者:** {row['報告者名']}")
                    st.write(f"**体温:** {row['体温']} C")
                    st.write(f"**症状:** {row['主な症状']}")
                    
                    # コメント入力と更新
                    comment = st.text_area(f"指示・コメントを入力 ({row['個体識別番号']})", key=f"com_{idx}")
                    if st.form_submit_button or st.button(f"対応完了にする ({row['個体識別番号']})", key=f"btn_{idx}"):
                        # 該当行のステータスとコメントを更新
                        df.at[idx, "確認ステータス"] = "対応完了"
                        df.at[idx, "獣医師コメント"] = comment
                        conn.update(worksheet="問診記録", data=df)
                        st.success("ステータスを更新しました。")
                        st.rerun()
                    st.markdown("---")
    except Exception as e:
        st.error(f"データ読み込みエラー: {e}")
