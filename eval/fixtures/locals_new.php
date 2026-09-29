<?php

namespace Eval\Locals;

use Eval\Mail\Emails;
use Eval\Traps\Cache;

final class Factory
{
    public function direct(string $user): void
    {
        $mailer = new Emails();
        $mailer->reminderSubmitWeek($user);
    }

    public function reassigned(string $user): void
    {
        $mailer = new Emails();
        $mailer = new Cache();
        $mailer->get($user);
    }

    public function fromCallable(callable $make, string $user): void
    {
        $mailer = $make();
        $mailer->reminderSubmitWeek($user);
    }
}
