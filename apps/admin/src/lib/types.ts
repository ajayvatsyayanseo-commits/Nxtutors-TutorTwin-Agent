/**
 * Response shapes served by the TutorTwin admin API.
 *
 * Hand-written rather than generated: the set is small, and a hand-written type
 * that omits a field is a compile error at the use site, whereas a generated
 * `any` is a runtime surprise. Every one of these mirrors a Pydantic model in
 * `src/tutortwin/api/routes/admin/`.
 */

export type AdminRole =
  | "SUPER_ADMIN"
  | "ADMIN"
  | "ACADEMIC_ADMIN"
  | "SUPPORT"
  | "TUTOR_VIEWER"
  | "USAGE_VIEWER";

/** The signed-in operator. Safe for a client component: it holds no token. */
export interface AdminActor {
  admin_id: string;
  email: string;
  role: AdminRole;
  permissions: string[];
  must_change_password: boolean;
}

export interface Page<T> {
  total: number;
  page: number;
  page_size: number;
  items: T[];
}

export interface DashboardCounts {
  students_total: number;
  students_active_in_window: number;
  requests: number;
  requests_failed: number;
  questions_answered: number;
  media_objects: number;
  mock_tests: number;
  model_calls: number;
  cost_micros: number;
  verifier_calls: number;
  quota_blocked: number;
  jobs_failed: number;
  extraction_cache_entries: number;
  local_extractions: number;
  vision_escalations: number;
  rag_sources: number;
  rag_chunks: number;
  input_tokens: number;
  cached_input_tokens: number;
  /** Null when nothing completed in the window — not zero. */
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
}

export interface CostByAlias {
  model_alias: string;
  provider: string;
  calls: number;
  cost_micros: number;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
}

export interface Dashboard {
  window_days: number;
  generated_at: string;
  counts: DashboardCounts;
  cost_by_alias: CostByAlias[];
  failures_by_code: { error_code: string; count: number }[];
}

export interface HealthSummary {
  conversations_open: number;
  jobs_pending: number;
  jobs_failed: number;
  attempts_awaiting_grade: number;
}

export interface StudentSummary {
  id: string;
  external_identity_type: string;
  external_identity_value: string;
  display_name: string | null;
  status: string;
  plan_code: string | null;
  entitlement_status: string | null;
  tutor_id: string | null;
  tutor_name: string | null;
  created_at: string;
}

export interface StudentDetail {
  student: StudentSummary;
  entitlements: {
    id: string;
    plan_code: string;
    status: string;
    source: string;
    starts_at: string | null;
    ends_at: string | null;
    fetched_at: string;
  }[];
  usage: {
    requests: number;
    model_calls: number;
    cost_micros: number;
    input_tokens: number;
    output_tokens: number;
  };
  conversations: {
    id: string;
    status: string;
    source: string;
    created_at: string;
    last_activity_at: string;
  }[];
  documents: {
    id: string;
    title: string;
    kind: string;
    visibility: string;
    status: string;
    chunk_count: number;
    content_sha256: string;
    deleted_at: string | null;
    created_at: string;
  }[];
  assessments: {
    id: string;
    kind: string;
    title: string;
    topic: string | null;
    duration_minutes: number;
    total_marks: number;
    truncated_reason: string | null;
    created_at: string;
  }[];
  progress: {
    topic: string;
    attempts: number;
    correct: number;
    hints_used: number;
    last_seen_at: string;
  }[];
  memories: {
    id: string;
    kind: string;
    statement: string;
    confidence: string;
    evidence: string;
    observed_count: number;
    derived_by: string;
    updated_at: string;
  }[];
  decks: { id: string; name: string; topic: string | null; cards: number }[];
  artifacts: {
    id: string;
    kind: string;
    artifact_format: string;
    generated_by: string;
    sha256: string;
    created_at: string;
  }[];
}

export interface ConversationSummary {
  id: string;
  subject_id: string;
  student_identity: string;
  tutor_name: string | null;
  status: string;
  source: string;
  message_count: number;
  created_at: string;
  last_activity_at: string;
}

export interface ConversationTimeline {
  conversation: ConversationSummary;
  messages: {
    id: string;
    role: string;
    input_type: string;
    text: string | null;
    capability: string | null;
    media: Record<string, unknown> | null;
    safety_flags: Record<string, unknown>;
    created_at: string;
  }[];
  requests: {
    id: string;
    request_id: string;
    correlation_id: string;
    message_type: string;
    source: string;
    status: string | null;
    error_code: string | null;
    /** Null while the request is still in flight — never zero. */
    latency_ms: number | null;
    occurred_at: string;
    created_at: string;
  }[];
  outbound: {
    id: string;
    action_type: string;
    delivery_status: string;
    payload: Record<string, unknown>;
    created_at: string;
  }[];
  model_calls: {
    id: string;
    provider: string;
    model_alias: string;
    model_id: string | null;
    capability: string | null;
    is_verification: boolean;
    input_tokens: number;
    output_tokens: number;
    cached_tokens: number;
    cost_micros: number;
    rate_version: string | null;
    created_at: string;
  }[];
  retrievals: {
    id: string;
    performed: boolean;
    skip_reason: string | null;
    top_k: number;
    returned: number;
    candidates_scanned: number;
    embedding_calls: number;
    query_ms: number;
    /** Chunk ids only. Vectors are never sent to the control plane. */
    chunk_ids: string[];
    created_at: string;
  }[];
  cost_micros: number;
}

export interface DocumentSummary {
  id: string;
  title: string;
  kind: string;
  visibility: string;
  status: string;
  owner_subject_id: string | null;
  owner_identity: string | null;
  tutor_id: string | null;
  chunk_count: number;
  content_sha256: string;
  parser_version: string;
  chunker_version: string;
  embedding_model: string;
  deleted_at: string | null;
  created_at: string;
}

export interface DocumentDetail {
  document: DocumentSummary;
  chunks: {
    id: string;
    ordinal: number;
    page_number: number | null;
    section: string | null;
    token_estimate: number;
    text_preview: string;
    has_embedding: boolean;
  }[];
}

export interface TutorSummary {
  id: string;
  display_name: string;
  status: string;
  active_persona_version: number | null;
  persona_versions: number;
  assigned_students: number;
  created_at: string;
}

export interface PersonaVersion {
  id: string;
  version: number;
  is_active: boolean;
  persona: Record<string, unknown>;
  created_at: string;
}

export interface TutorDetail {
  tutor: TutorSummary;
  personas: PersonaVersion[];
  students: {
    subject_id: string;
    identity: string;
    display_name: string | null;
    is_active: boolean;
    assigned_at: string;
  }[];
}

export interface PlanView {
  plan_code: string;
  version: number;
  allows_paid_ai: boolean;
  features: Record<string, unknown>;
  limits: Record<string, unknown>;
  created_at: string;
}

export interface ModelRouteView {
  id: string;
  model_alias: string;
  provider: string;
  model_id: string;
  is_active: boolean;
  input_cost_micros_per_1k: number;
  output_cost_micros_per_1k: number;
  rate_version: string;
  created_at: string;
}

export interface ModelCatalogResponse {
  aliases: string[];
  routes: ModelRouteView[];
  provider_key_configured: Record<string, boolean>;
}

export interface PromptVersionView {
  id: string;
  block_key: string;
  version: number;
  status: string;
  body: string;
  reason: string | null;
  author_admin_id: string | null;
  activated_at: string | null;
  created_at: string;
}

export interface FlagView {
  key: string;
  enabled: boolean;
  description: string | null;
  is_kill_switch: boolean;
  updated_at: string;
}

export interface JobView {
  id: string;
  job_type: string;
  state: string;
  attempts: number;
  max_attempts: number;
  next_retry_at: string | null;
  last_error: string | null;
  correlation_id: string | null;
  owner_subject_id: string | null;
  owner_identity: string | null;
  media_object_id: string | null;
  media_state: string | null;
  payload: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface CostBucket {
  key: string;
  label: string;
  calls: number;
  cost_micros: number;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
}

export interface CostResponse {
  window_days: number;
  group_by: string;
  total_cost_micros: number;
  total_calls: number;
  cached_tokens: number;
  buckets: CostBucket[];
}

export interface AuditEntry {
  id: string;
  actor_type: string;
  actor_id: string | null;
  actor_email: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  reason: string | null;
  high_risk: boolean;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface AdminSummary {
  id: string;
  email: string;
  display_name: string;
  role: string;
  status: string;
  must_change_password: boolean;
  last_login_at: string | null;
  created_at: string;
}

export interface AssessmentSummary {
  id: string;
  subject_id: string;
  student_identity: string;
  kind: string;
  title: string;
  topic: string | null;
  duration_minutes: number;
  total_marks: number;
  truncated_reason: string | null;
  attempts: number;
  created_at: string;
}

export interface PaymentView {
  id: string;
  order_id: string;
  gateway: string;
  gateway_payment_id: string | null;
  amount_paise: number;
  currency: string;
  plan_code: string;
  plan_days: number;
  status: string;
  /** Null on a PAID row means charged but never granted access. */
  activated_at: string | null;
  created_at: string;
  student_name: string;
  whatsapp_number: string;
  contact_phone: string | null;
  tutor_name: string;
  subject: string;
  location: string | null;
  signup_id: string;
  signup_status: string;
  subject_id: string | null;
}

export interface SignupView {
  id: string;
  student_name: string;
  whatsapp_number: string;
  contact_phone: string | null;
  tutor_name: string;
  subject: string;
  location: string | null;
  status: string;
  subject_id: string | null;
  created_at: string;
  payment_count: number;
  paid: boolean;
}

export interface RevenueSummary {
  signups: number;
  paid_signups: number;
  abandoned_signups: number;
  orders: number;
  paid_orders: number;
  gross_paise: number;
  awaiting_activation: number;
}
