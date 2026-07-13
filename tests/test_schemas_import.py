"""验证所有 schemas 模块可正确导入（Task 6）。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hermes"))


def test_chat_schemas():
    from hermes.schemas.chat import ChatRequest, CancelRequest, ChatResponse
    req = ChatRequest(message="test", session_id="s1")
    assert req.message == "test"
    cancel = CancelRequest(session_id="s1")
    assert cancel.mode == "immediate"
    resp = ChatResponse(session_id="s1", response="hi", timestamp="now")
    assert resp.response == "hi"


def test_common_schemas():
    from hermes.schemas.common import (
        HealthResponse, SessionItem, SessionListResponse,
        SessionTitleUpdate, MessageItem, MessageListResponse,
        DeleteSessionResponse, FlushResponse,
    )
    item = SessionItem(id="s1", created_at="now", updated_at="now")
    assert item.title is None
    msg = MessageItem(role="user", content="hi", created_at="now")
    assert msg.tool_name is None
    title = SessionTitleUpdate(title="Test")
    assert title.title == "Test"
    flush = FlushResponse(status="ok", message="done", timestamp="now")
    assert flush.pending_count == 0


def test_config_schemas():
    from hermes.schemas.config import ConfigResponse, ConfigUpdateRequest, ConfigUpdateResponse
    resp = ConfigResponse(config={"key": "val"})
    assert resp.config == {"key": "val"}
    req = ConfigUpdateRequest(config={"key": "val"})
    assert req.config == {"key": "val"}
    upd = ConfigUpdateResponse(status="ok", message="done", needs_restart=False)
    assert upd.needs_restart is False


def test_approvals_schemas():
    from hermes.schemas.approvals import (
        ApprovalResolveRequest, ApprovalResolveResponse,
        ApprovalListItem, ApprovalListResponse,
    )
    req = ApprovalResolveRequest(decision="approve")
    assert req.reason is None
    item = ApprovalListItem(
        approval_id="a1", tool_name="search", tool_input={},
        reason="test", created_at="now",
    )
    lst = ApprovalListResponse(pending=[item])
    assert len(lst.pending) == 1


def test_schedules_schemas():
    from hermes.schemas.schedules import (
        ScheduleCreateRequest, ScheduleUpdateRequest,
        ScheduleListResponse, ScheduleResponse,
    )
    req = ScheduleCreateRequest(name="test", cron="* * * * *", task="do thing")
    assert req.enabled is True
    upd = ScheduleUpdateRequest(name="new")
    assert upd.cron is None
    lst = ScheduleListResponse()
    assert lst.schedules == []
    resp = ScheduleResponse(schedule_id="s1", message="ok")
    assert resp.schedule_id == "s1"


def test_files_schemas():
    from hermes.schemas.files import (
        FileUploadResponse, FileItem, FileListResponse, FileDeleteResponse,
    )
    up = FileUploadResponse(file_id="f1")
    assert up.is_dup is False
    item = FileItem(
        file_id="f1", original_name="test.txt", size=100, type="text/plain",
        etl_status="pending", uploaded_at="now", last_accessed="now",
    )
    assert item.chunk_count == 0
    lst = FileListResponse(files=[item])
    assert len(lst.files) == 1
    dele = FileDeleteResponse(status="ok", file_id="f1")
    assert dele.details == {}


def test_proposals_schemas():
    from hermes.schemas.proposals import ProposalModifyRequest
    req = ProposalModifyRequest()
    assert req.schedule_config_updates is None
    assert req.requested_tools is None


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
