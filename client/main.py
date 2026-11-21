import json
import streamlit as st
import os
from pathlib import Path
from dotenv import load_dotenv
import requests
from requests.auth import HTTPBasicAuth

root_dir = Path(__file__).resolve().parent.parent
load_dotenv(root_dir / ".env")

URL = os.getenv("BASE_URL", "http://127.0.0.1:8000")

st.set_page_config(page_title="MediGuide", layout="centered")

if os.getenv("MEDIGUIDE_WALKTHROUGH") == "1":
    st.info("Local walkthrough with synthetic data and scripted answers. Use the bundled specimen log.")
    sample_path = root_dir / "demo" / "specimen-log.pdf"
    st.download_button("Download sample report", sample_path.read_bytes(), file_name=sample_path.name,
                       mime="application/pdf")

if "username" not in st.session_state:
    st.session_state.username = ""
    st.session_state.password = ""
    st.session_state.role = ""
    st.session_state.logged_in = False
    st.session_state.mode = "auth"


def get_auth():
    return HTTPBasicAuth(st.session_state.username, st.session_state.password)


def auth_ui():
    st.title("MediGuide")
    st.subheader("Login or Signup")

    tab1, tab2 = st.tabs(["Login", "Signup"])

    with tab1:
        username = st.text_input("Username", key="login_user")
        password = st.text_input("Password", type="password", key="login_password")
        if st.button("Login"):
            res = requests.post(f"{URL}/login", auth=HTTPBasicAuth(username, password))
            if res.status_code == 200:
                user_data = res.json()
                st.session_state.username = username
                st.session_state.password = password
                st.session_state.role = user_data["role"]
                st.session_state.logged_in = True
                st.session_state.mode = "chat"
                st.rerun()
            else:
                st.error(res.json().get("detail", "Login failed"))

    with tab2:
        new_user = st.text_input("New Username", key="signup_user")
        new_pass = st.text_input("New Pass", type="password", key="signup_pass")
        st.caption("New accounts are registered as patients.")
        if st.button("Signup"):
            payload = {"username": new_user, "password": new_pass, "role": "patient"}
            res = requests.post(f"{URL}/signup", json=payload)

            if res.status_code == 200:
                st.success("Signup successful! You can login.")
            else:
                st.error(res.json().get("detail", "Signup failed"))


# Upload PDF (Admin only)
def upload_docs():
    st.subheader("Upload PDF for specific Role")
    uploaded_file = st.file_uploader("Choose a file to upload.")
    role_for_doc = st.selectbox("Target Role for docs", ["doctor", "nurse", "patient", "other"])

    if st.button("Upload Document"):
        if uploaded_file:
            files = {"file": (uploaded_file.name, uploaded_file.getvalue(), "application/pdf")}
            data = {"role": role_for_doc}
            res = requests.post(f"{URL}/docs", files=files, data=data, auth=get_auth())

            if res.status_code == 200:
                doc_info = res.json()
                st.success(f"uploaded: {uploaded_file.name}")
                st.info(f"Doc Id: {doc_info['doc_id']}, Access: {doc_info['accessible_to']}")
                if doc_info.get("warning"):
                    st.warning(doc_info["warning"])
            else:
                st.error(res.json().get("detail", "Upload failed!"))
        else:
            st.warning("Please upload a file")


# Upload medical report and get a summary
def upload_report():
    st.subheader("Upload your medical report for summary")
    uploaded_file = st.file_uploader("Upload a medical PDF report", type=["pdf"], key="report_uploader")

    if st.button("Upload and Summarize", key="upload_report"):
        if uploaded_file:
            files = {"file": (uploaded_file.name, uploaded_file.getvalue(), "application/pdf")}
            res = requests.post(f"{URL}/docs/report", files=files, auth=get_auth())

            if res.status_code == 202:
                st.success("Report saved. Open My Reports to check its progress.")
            else:
                st.error(res.json().get("detail", "Upload failed."))
        else:
            st.warning("Please upload a PDF file.")


# View uploaded reports and summaries
def view_reports(report_type=None):
    search_query = st.text_input(
        "Search by filename or upload date (YYYY-MM-DD)",
        value="",
        key=f"search_{report_type}" if report_type else "search_all"
    ).strip().lower()

    st.button("Refresh reports", key=f"refresh_{report_type}")
    res = requests.get(f"{URL}/docs/reports", auth=get_auth())
    if res.status_code != 200:
        st.error(res.json().get("error", "Unable to load reports."))
        return

    data = res.json()
    reports = data.get("reports", [])
    if not reports:
        st.info("No uploaded reports found.")
        return

    filtered = [
        report for report in reports
        if (report_type is None or report.get("type") == report_type)
        and (
            not search_query
            or search_query in report.get("filename", "").lower()
            or search_query in report.get("uploaded_at", "").lower()
        )
    ]

    if not filtered:
        st.info("No reports found for this filter.")
        return

    grouped = {}
    for report in filtered:
        report_type_key = report.get("type", "unknown")
        grouped.setdefault(report_type_key, []).append(report)

    if "reference" in grouped:
        st.markdown("### Reference Documents")
        for report in grouped["reference"]:
            with st.expander(f"{report.get('filename')} ({report.get('role')})"):
                st.write(f"**Uploaded by:** {report.get('owner')}")
                st.write(f"**Role:** {report.get('role')}")
                st.write(f"**Uploaded at:** {report.get('uploaded_at')}")
                st.write("This document is stored as reference material for role-based access.")

    if "patient_report" in grouped:
        st.markdown("### Patient Reports")
        for report in grouped["patient_report"]:
            with st.expander(f"{report.get('filename')} ({report.get('uploaded_at')})"):
                st.write(f"**Uploaded by:** {report.get('owner')}")
                st.write(f"**Role:** {report.get('role')}")
                st.write(f"**Uploaded at:** {report.get('uploaded_at')}")
                status = report.get("status", "complete")
                labels = {
                    "queued": "Waiting to index",
                    "indexing": "Indexing report",
                    "indexed": "Ready for questions. Summary queued.",
                    "summarizing": "Ready for questions. Summary in progress.",
                    "index_failed": "Indexing failed. Check that the PDF has readable text.",
                    "summary_failed": "Ready for questions. Summary generation failed.",
                }
                if status in labels:
                    st.info(labels[status])
                    if status.endswith("_failed") and report["owner"] == st.session_state.username:
                        if st.button("Retry processing", key=f"retry_{report['doc_id']}"):
                            response = requests.post(f"{URL}/docs/reports/{report['doc_id']}/retry", auth=get_auth(), timeout=10)
                            if response.status_code == 202:
                                st.rerun()
                            st.error("Could not retry. Refresh the report status.")
                elif report.get("summary"):
                    st.markdown("**Summary**")
                    st.write(report["summary"])

    if "unknown" in grouped:
        st.markdown("### Other Reports")
        for report in grouped["unknown"]:
            with st.expander(f"{report.get('filename')} ({report.get('type')})"):
                st.write(f"**Uploaded by:** {report.get('owner')}")
                st.write(f"**Role:** {report.get('role')}")
                st.write(f"**Uploaded at:** {report.get('uploaded_at')}")
                st.markdown("**Summary**")
                st.write(report.get('summary', 'No summary available.'))
    return


# chat interface
def chat_interface():
    st.subheader("Ask a medical healthcare question:")
    use_groq = False
    if os.getenv("MEDIGUIDE_WALKTHROUGH") != "1":
        use_groq = st.checkbox("Send this question and the retrieved report excerpts to Groq", key="use_groq")
    msg = st.text_input("Your query")

    if st.button("Send"):
        if not msg.strip():
            st.warning("Enter a question.")
            return

        reply = {"answer": "", "sources": [], "complete": False}
        st.session_state.last_reply = reply
        live = st.empty()
        try:
            with requests.post(f"{URL}/chat/stream", json={"query": msg, "top_k": 5,
                               "provider": "groq" if use_groq else "ollama"},
                               auth=get_auth(), stream=True, timeout=(5, 90)) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line:
                        continue
                    item = json.loads(line)
                    if item["type"] == "text":
                        reply["answer"] += item["text"]
                        live.text(reply["answer"])
                    elif item["type"] == "sources":
                        reply["sources"] = item["sources"]
                    elif item["type"] == "done":
                        reply["complete"] = True
                        break
                    elif item["type"] == "error":
                        break
        except (requests.RequestException, ValueError, KeyError):
            pass
        live.empty()

    reply = st.session_state.get("last_reply")
    if reply:
        st.text(reply["answer"])
        if not reply["complete"]:
            st.warning("Answer incomplete. Send the question again to retry.")
        for number, source in enumerate(reply["sources"], 1):
            page = source.get("page")
            label = f"[{number}] {source['source']}"
            if isinstance(page, int):
                label += f" - Page {page + 1}"
            with st.expander(label):
                st.text(source["excerpt"])
                if isinstance(page, int) and st.button("Read full page", key=f"page_{number}"):
                    try:
                        response = requests.get(f"{URL}/docs/reports/{source['doc_id']}/pages/{page + 1}",
                                                auth=get_auth(), timeout=(5, 30))
                        response.raise_for_status()
                        st.text(response.json()["text"])
                    except (requests.RequestException, ValueError, KeyError):
                        st.warning("The original page is unavailable.")


if not st.session_state.logged_in:
    auth_ui()

else:
    st.title(f"Welcome, {st.session_state.username}")
    st.markdown(f"**Role**: {st.session_state.role}")

    if st.button("Logout"):
        st.session_state.pop("last_reply", None)
        st.session_state.pop("use_groq", None)
        st.session_state.logged_in = False
        st.session_state.username = ""
        st.session_state.password = ""
        st.session_state.role = ""
        st.session_state.mode = "auth"
        st.rerun()

    if st.session_state.role == "admin":
        st.markdown("## Admin dashboard")
        tab1, tab2, tab3, tab4 = st.tabs(["Upload Reference Docs", "Reference Docs", "Patient Reports", "Chat"])

        with tab1:
            st.info("Upload role-specific reference documents for doctors, nurses, patients, or others.")
            upload_docs()

        with tab2:
            st.info("View all reference documents uploaded by admins.")
            view_reports(report_type="reference")

        with tab3:
            st.info("View all patient reports and summaries.")
            view_reports(report_type="patient_report")

        with tab4:
            chat_interface()

        with st.expander("Compare AI providers on a fictional report"):
            route = st.selectbox("Provider", ["auto", "groq", "gemini", "openrouter"])
            case = st.selectbox("Example", ["sample_a", "sample_b", "missing"])
            if st.button("Run provider demo"):
                try:
                    response = requests.post(f"{URL}/chat/demo/{route}/{case}", auth=get_auth(), timeout=(5, 45))
                    response.raise_for_status()
                    demo = response.json()
                    st.text(demo["document"])
                    st.text(demo["question"])
                    for attempt in demo["attempts"]:
                        provider = attempt["provider"]
                        if attempt["status"] == "unconfigured":
                            st.info(f"{provider} needs a server API key and model ID.")
                        elif attempt["status"] != "ok":
                            st.warning(f"{provider} request failed ({attempt['status']}).")
                        else:
                            answer = attempt["answer"]
                            if answer["value"] is None:
                                st.write("The model found no recorded value.")
                            else:
                                st.text(f"{answer['value']} {answer['unit']} (page {answer['page']})")
                            st.caption(f"{provider} / {attempt['model']} | First text {attempt['first_text_s']:.2f}s"
                                       f" | Complete {attempt['total_s']:.2f}s")
                            if attempt["value_unit_correct"] and attempt["citation_correct"]:
                                st.success("Value, unit and page match the fixture.")
                            else:
                                st.warning("The answer differs from the fixture.")
                except (requests.RequestException, ValueError):
                    st.error("The provider demo could not finish.")

    elif st.session_state.role == "patient":
        st.markdown("## Patient dashboard")
        tab1, tab2, tab3 = st.tabs(["Upload Report", "My Reports", "Chat"])

        with tab1:
            st.info("Upload your medical report PDF and receive a concise summary.")
            upload_report()

        with tab2:
            st.info("Review your own uploaded reports and summaries.")
            view_reports()

        with tab3:
            chat_interface()

    else:
        # FIX #1: st.tab() doesn't exist — use st.tabs() which returns a list
        st.markdown("## User dashboard")
        (tab1,) = st.tabs(["Chat"])
        with tab1:
            st.info("Chat access only. Medical report upload is available for patients.")
            chat_interface()
