"""验证 schemas 模块可正确导入。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

def test_schemas_import():
    from schemas.chat import ChatRequest, ChatResponse, CancelRequest
    from schemas.config import ConfigResponse, ConfigUpdateRequest, ConfigUpdateResponse
    from schemas.schedules import ScheduleCreateRequest, ScheduleUpdateRequest, ScheduleListResponse, ScheduleResponse
    from schemas.approvals import ApprovalResolveRequest, ApprovalResolveResponse, ApprovalListItem, ApprovalListResponse
    from schemas.proposals import ProposalModifyRequest
    from schemas.files import FileUploadResponse, FileItem, FileListResponse, FileDeleteResponse
    from schemas.common import HealthResponse, SessionItem, SessionListResponse, SessionTitleUpdate, MessageItem, MessageListResponse, DeleteSessionResponse, FlushResponse
    assert ChatRequest is not None
    assert ChatResponse is not None
    assert ConfigResponse is not None
    assert ScheduleListResponse is not None
    assert ApprovalListResponse is not None
    assert ProposalModifyRequest is not None
    assert FileUploadResponse is not None
    assert DeleteSessionResponse is not None
    assert FlushResponse is not None
