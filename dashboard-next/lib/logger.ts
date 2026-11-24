import pino from 'pino';

export const logger = pino({
  level: process.env.LOG_LEVEL || 'info',
  redact: {
    paths: ['*.password', '*.token', '*.key', '*.secret', '*.authorization', '*.apiKey'],
    censor: '[REDACTED]',
  },
});
