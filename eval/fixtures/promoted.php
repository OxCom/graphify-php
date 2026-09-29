<?php

namespace Eval\Promoted;

use Eval\Mail\Emails;

final class Reminder
{
    public function __construct(private readonly Emails $emails)
    {
    }

    public function weekly(string $user): void
    {
        $this->emails->reminderSubmitWeek($user);
    }

    public function monthly(string $user): void
    {
        $this->emails->reminderSubmitMonth($user);
    }
}
