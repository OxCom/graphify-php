<?php

namespace Eval\Chained;

use Eval\Mail\Emails;

final class Registry
{
    public function __construct(private readonly Provider $provider)
    {
    }

    public function run(string $user): void
    {
        $this->provider->mailer()->reminderSubmitWeek($user);
    }

    public function firstHop(): void
    {
        $this->provider->mailer();
    }
}

final class Provider
{
    public function mailer(): Emails
    {
        return new Emails();
    }
}
