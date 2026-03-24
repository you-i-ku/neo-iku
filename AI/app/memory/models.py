"""SQLAlchemyモデル定義"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship, DeclarativeBase


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    ended_at = Column(DateTime, nullable=True)
    summary = Column(Text, nullable=True)
    is_imported = Column(Boolean, default=False)
    source = Column(String(20), default="chat")  # "chat" or "autonomous"
    trigger = Column(String(20), nullable=True)   # "timer" / "energy" / "manual" / None(chat)

    messages = relationship("Message", back_populates="conversation", order_by="Message.created_at")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    role = Column(String(20), nullable=False)  # "user" or "assistant"
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    conversation = relationship("Conversation", back_populates="messages")


class IkuLog(Base):
    """イクの過去ログ（過去ログ/フォルダの12ファイルから取り込み）"""
    __tablename__ = "iku_logs"

    id = Column(Integer, primary_key=True)
    file_name = Column(String(200), nullable=False)
    role = Column(String(20), nullable=False)  # "user" or "assistant"
    content = Column(Text, nullable=False)
    sequence = Column(Integer, nullable=False)  # ファイル内の順序


class ToolAction(Base):
    """ツール実行履歴（メタ認知の基盤）"""
    __tablename__ = "tool_actions"

    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, nullable=True)
    tool_name = Column(String(100), nullable=False)
    arguments = Column(Text, nullable=False)       # JSON文字列
    result_summary = Column(Text, nullable=False)   # 結果の先頭500文字
    expected_result = Column(Text, nullable=True)    # 実行前の予測（メタ認知用）
    status = Column(String(20), default="success")  # success / error
    execution_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class SelfModelSnapshot(Base):
    """self_model.jsonの変更履歴（自己進化計測用）"""
    __tablename__ = "self_model_snapshots"

    id = Column(Integer, primary_key=True)
    content = Column(Text, nullable=False)          # JSON全体
    changed_key = Column(String(100), nullable=True)  # 変更されたキー
    created_at = Column(DateTime, default=datetime.utcnow)


class MemorySummary(Base):
    """将来イク自身が自発的に生成する要約用（今は未使用）"""
    __tablename__ = "memory_summaries"

    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=True)
    content = Column(Text, nullable=False)
    keywords = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    source = Column(String(50), default="chat")
