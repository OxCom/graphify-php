<?php

namespace Eval\Secrets;

use Eval\Mail\Emails;

/**
 * Every literal below is a planted fake, recognisable by its CANARY marker. Nothing here is a
 * credential for anything; the point is that these exact strings must not survive into any
 * artifact the package writes. Identifiers around them (class, method, parameter, constant
 * names) may be indexed — that is the rule this fixture makes testable.
 */

#[Sensitive(token: 'CANARY-attr-arg-7c1f0d2b9e')]
final class Mailer
{
    public const FALLBACK_DSN = 'smtp://canary-user:CANARY-dsn-pw-4a91f3@mail.invalid:587/?tls=1';

    public const API_TOKEN = 'cnry_live_CANARY0000token0000literal0000deadbeef';

    public function __construct(private readonly Emails $emails)
    {
    }

    public function dsn(): string
    {
        return getenv('MAILER_DSN') ?: self::FALLBACK_DSN;
    }

    public function apiKey(): string
    {
        return $_ENV['MAILER_API_KEY'] ?? 'CANARY-env-default-53bd8ac017';
    }

    public function notify(string $user): void
    {
        $this->emails->reminderSubmitWeek($user);
    }
}
