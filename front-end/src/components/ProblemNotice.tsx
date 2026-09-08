import type { ApiProblem } from '../api/types'

export function ProblemNotice({ problem, onDismiss }: { problem: ApiProblem; onDismiss?: () => void }) {
  const heading = problem.code === 'rate_limited' ? 'Rate limit reached'
    : problem.code === 'quota_exceeded' || problem.code === 'token_quota_exceeded' ? 'Token quota reached'
      : problem.code === 'authentication_required' ? 'Reconnect your account'
        : 'Request needs attention'
  return (
    <div className="problem-notice" role="alert">
      <div>
        <strong>{heading}</strong>
        <p>{problem.message}</p>
        {problem.retryAfter !== undefined && <p>Wait at least {Math.ceil(problem.retryAfter)} seconds before trying again. Requests are not retried automatically.</p>}
        {problem.requestId && <p className="request-id">Request ID: {problem.requestId}</p>}
      </div>
      {onDismiss && <button className="icon-button" type="button" onClick={onDismiss} aria-label="Dismiss error">&times;</button>}
    </div>
  )
}
