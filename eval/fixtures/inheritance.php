<?php

namespace Eval\Inherit;

use Eval\Mail\Emails;

abstract class BaseNotifier
{
    protected readonly Emails $emails;

    public function __construct(Emails $emails)
    {
        $this->emails = $emails;
    }

    public function notify(string $user): void
    {
        $this->emails->reminderSubmitWeek($user);
    }
}

final class WeeklyNotifier extends BaseNotifier
{
    public function notify(string $user): void
    {
        parent::notify($user);
        $this->emails->reminderSubmitMonth($user);
    }
}
