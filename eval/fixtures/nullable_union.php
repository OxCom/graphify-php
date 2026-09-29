<?php

namespace Eval\Variance;

use Eval\Mail\Emails;
use Eval\Traps\Cache;

final class Mixed
{
    public function __construct(
        private readonly ?Emails $maybeMailer,
        private readonly Emails|Cache $either,
    ) {
    }

    public function nullableCall(string $user): void
    {
        $this->maybeMailer?->reminderSubmitWeek($user);
    }

    public function unionCall(string $key): void
    {
        $this->either->get($key);
    }
}
