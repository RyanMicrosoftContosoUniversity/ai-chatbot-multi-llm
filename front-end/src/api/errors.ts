import type { ApiProblem } from './types'

export class ApiError extends Error implements ApiProblem {
  readonly code: string
  readonly requestId?: string
  readonly retryAfter?: number
  readonly status?: number

  constructor(problem: ApiProblem, status?: number) {
    super(problem.message)
    this.name = 'ApiError'
    this.code = problem.code
    this.requestId = problem.requestId
    this.retryAfter = problem.retryAfter
    this.status = status
  }
}

export class StreamError extends ApiError {
  constructor(message: string, code = 'invalid_stream') {
    super({ code, message })
    this.name = 'StreamError'
  }
}

export function isAbort(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError'
}

export function problemFrom(error: unknown): ApiProblem {
  if (error instanceof ApiError) return error
  return {
    code: 'request_failed',
    message: error instanceof Error ? error.message : 'The request failed. Please try again explicitly.',
  }
}
