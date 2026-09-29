<?php

namespace Eval\Traits;

use Eval\Mail\Emails;

trait SendsMail
{
    protected readonly Emails $emails;

    public function dispatch(string $user): void
    {
        $this->emails->reminderSubmitWeek($user);
    }
}

final class Campaign
{
    use SendsMail;

    public function __construct(Emails $emails)
    {
        $this->emails = $emails;
    }

    public function run(string $user): void
    {
        $this->dispatch($user);
    }
}
