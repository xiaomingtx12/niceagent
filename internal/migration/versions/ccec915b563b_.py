"""重建测试环境的会话与执行表，清理旧业务历史。

本 revision 经用户确认允许丢弃测试历史；不适用于需要保留历史的数据库。
清理旧问答、执行、事件、测评、微信消息和会话，保留应用、账号及终端用户。
LangGraph 自有表不在本迁移范围内；旧会话删除后不再使用其 checkpoint。
删除的历史和身份旧字段无法恢复，本 revision 不提供 downgrade。

Revision ID: ccec915b563b
Revises: d72c6dd9cab9
Create Date: 2026-09-15 18:55:53.342083

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'ccec915b563b'
down_revision = 'd72c6dd9cab9'
branch_labels = None
depends_on = None


def upgrade():
    """重建测试历史表，并按当前 ORM 调整字段、关联与索引。"""
    # 先清理逻辑关联记录，避免旧执行/问答删除后留下悬空引用。
    op.execute(sa.text('DELETE FROM agent_evaluation'))
    op.execute(sa.text('DELETE FROM wechat_message'))
    op.create_table('conversation_turn',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v4()'), nullable=False),
    sa.Column('app_id', sa.UUID(), nullable=False),
    sa.Column('conversation_id', sa.UUID(), nullable=False),
    sa.Column('invoke_from', sa.String(length=32), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=False),
    sa.Column('query', sa.Text(), server_default=sa.text("''::text"), nullable=False),
    sa.Column('image_urls', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('attachments', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('answer', sa.Text(), server_default=sa.text("''::text"), nullable=False),
    sa.Column('latency', sa.Float(), server_default=sa.text('0.0'), nullable=False),
    sa.Column('is_deleted', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('status', sa.String(length=16), server_default=sa.text("'normal'"), nullable=False),
    sa.Column('error', sa.Text(), server_default=sa.text("''::text"), nullable=False),
    sa.Column('total_token_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('input_token_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('output_token_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('cached_token_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('reasoning_token_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('model_call_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('tool_call_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('trace_id', sa.String(length=64), nullable=True),
    sa.Column('token_usage', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('total_price', sa.Numeric(precision=10, scale=7), server_default=sa.text('0.0'), nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP(0)'), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP(0)'), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_conversation_turn_id')
    )
    with op.batch_alter_table('conversation_turn', schema=None) as batch_op:
        batch_op.create_index('turn_conversation_created_id_idx', ['conversation_id', 'created_at', 'id'], unique=False, postgresql_where=sa.text('is_deleted = false'))
        batch_op.create_index('turn_created_by_idx', ['created_by'], unique=False)

    op.create_table('agent_execution',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v4()'), nullable=False),
    sa.Column('app_id', sa.UUID(), nullable=False),
    sa.Column('conversation_id', sa.UUID(), nullable=False),
    sa.Column('invoke_from', sa.String(length=32), nullable=False),
    sa.Column('turn_id', sa.UUID(), nullable=False),
    sa.Column('input_message_id', sa.String(length=255), nullable=True),
    sa.Column('status', sa.String(length=32), server_default=sa.text("'running'"), nullable=False),
    sa.Column('error_message', sa.Text(), server_default=sa.text("''::text"), nullable=False),
    sa.Column('execution_options', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('pending_inputs', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('input_history', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('display_state', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('last_event_sequence', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('executor_id', sa.UUID(), nullable=True),
    sa.Column('cancel_requested_at', sa.DateTime(), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP(0)'), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['turn_id'], ['conversation_turn.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_agent_execution_id'),
    sa.UniqueConstraint('turn_id', name='uq_agent_execution_turn_id')
    )
    with op.batch_alter_table('agent_execution', schema=None) as batch_op:
        batch_op.create_index('agent_execution_conversation_created_idx', ['conversation_id', 'created_at', 'id'], unique=False)
        batch_op.create_index('agent_execution_conversation_input_message_uq', ['conversation_id', 'input_message_id'], unique=True, postgresql_where=sa.text('input_message_id IS NOT NULL'))
        batch_op.create_index('agent_execution_conversation_occupied_uq', ['conversation_id'], unique=True, postgresql_where=sa.text("status IN ('running', 'waiting_input')"))

    op.create_table('agent_execution_event',
    sa.Column('execution_id', sa.UUID(), nullable=False),
    sa.Column('sequence', sa.Integer(), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP AT TIME ZONE 'UTC')"), nullable=False),
    sa.ForeignKeyConstraint(['execution_id'], ['agent_execution.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('execution_id', 'sequence', name='pk_agent_execution_event_run_sequence')
    )
    op.create_table('agent_execution_submission',
    sa.Column('execution_id', sa.UUID(), nullable=False),
    sa.Column('input_batch_id', sa.String(length=64), nullable=False),
    sa.Column('answers', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('accepted_sequence', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP AT TIME ZONE 'UTC')"), nullable=False),
    sa.ForeignKeyConstraint(['execution_id'], ['agent_execution.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('execution_id', 'input_batch_id', name='pk_agent_execution_submission_batch')
    )
    op.drop_table('agent_run_event')
    with op.batch_alter_table('agent_run', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('agent_run_conversation_executing_uq'), postgresql_where="((status)::text = ANY ((ARRAY['queued'::character varying, 'running'::character varying])::text[]))")
        batch_op.drop_index(batch_op.f('agent_run_conversation_input_message_uq'), postgresql_where='(input_message_id IS NOT NULL)')
        batch_op.drop_index(batch_op.f('agent_run_conversation_source_created_idx'))

    op.drop_table('agent_run')
    with op.batch_alter_table('message', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('message_conversation_created_id_idx'), postgresql_where='(is_deleted = false)')
        batch_op.drop_index(batch_op.f('message_created_by_idx'))

    op.drop_table('message')
    # 旧 checkpoint 不再关联可访问会话，避免新表为空但旧会话仍带有执行上下文。
    op.execute(sa.text('DELETE FROM conversation'))
    with op.batch_alter_table('agent_evaluation', schema=None) as batch_op:
        batch_op.alter_column('run_id', new_column_name='execution_id',
                              existing_type=sa.UUID(), existing_nullable=False)

    with op.batch_alter_table('app_config', schema=None) as batch_op:
        batch_op.drop_column('review_config')
        batch_op.drop_column('dialog_round')

    with op.batch_alter_table('app_config_version', schema=None) as batch_op:
        batch_op.drop_column('review_config')
        batch_op.drop_column('dialog_round')

    with op.batch_alter_table('conversation', schema=None) as batch_op:
        batch_op.alter_column('invoke_from',
               existing_type=sa.VARCHAR(length=255),
               type_=sa.String(length=32),
               server_default=None,
               existing_nullable=False,
               existing_server_default=sa.text("''::character varying"))
        batch_op.drop_column('summary')

    with op.batch_alter_table('end_user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('account_id', sa.UUID(), nullable=True))
        batch_op.drop_index(batch_op.f('end_user_tenant_id_idx'))
        batch_op.drop_index(batch_op.f('uq_end_user_app_external'))
        batch_op.create_index('uq_end_user_app_account', ['app_id', 'account_id'], unique=True)
        batch_op.drop_column('external_user_id')
        batch_op.drop_column('tenant_id')

    with op.batch_alter_table('wechat_message', schema=None) as batch_op:
        batch_op.alter_column('message_id', new_column_name='turn_id',
                              existing_type=sa.UUID(), existing_nullable=False)



def downgrade():
    """历史清理不可逆，禁止用空表重建伪装成数据回滚。"""
    raise RuntimeError(
        'ccec915b563b 已清理测试历史且删除旧身份字段，不支持 downgrade；'
        '如需回退，请恢复迁移前备份或重建测试数据库。'
    )
