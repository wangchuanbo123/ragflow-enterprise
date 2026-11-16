export interface OperationMetric {
  observations: number
  total: number
  mean: number
}

export interface OperationAlert {
  code: string
  message: string
  seconds?: number
  count?: number
}

export interface OperationsOverview {
  checked_at: string
  source: 'shared_redis' | 'process' | 'process_fallback'
  retention_days: number
  metrics: Record<string, OperationMetric>
  tasks: Record<string, number>
  oldest_pending_seconds: number
  database_pool: { size?: number; checkedin?: number; checkedout?: number; overflow?: number }
  process: { rss_bytes: number }
  disk: { total_bytes: number; free_bytes: number }
  memory: { percent: number; scope: 'host' | 'container' }
  backup: {
    status: string
    last_success_at?: string
    last_attempt_at?: string
    last_error?: string
    mirror_status?: string
    mirror_error?: string
    rpo_target_hours?: number
    rto_target_minutes?: number
    rto_verified?: boolean
  }
  alerts: OperationAlert[]
}
