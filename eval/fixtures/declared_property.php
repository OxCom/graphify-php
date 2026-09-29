<?php

namespace Eval\Declared;

use Eval\Mail\Emails;

final class Digest
{
    private readonly Emails $legacy;

    private ?Emails $optional = null;

    public function __construct(Emails $legacy)
    {
        $this->legacy = $legacy;
    }

    public function send(string $user): void
    {
        $this->legacy->reminderSubmitWeek($user);
    }

    public function maybeSend(string $user): void
    {
        $this->optional?->reminderSubmitMonth($user);
    }
}
