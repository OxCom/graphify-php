<?php

namespace Eval\CtorBody;

use Eval\Mail\Emails;

/**
 * The property carries no declared type; the constructor parameter does.
 * This predates promoted properties, so it is the common shape in older code.
 */
final class Untyped
{
    private $mailer;

    private $unknown;

    public function __construct(Emails $mailer, $unknown)
    {
        $this->mailer = $mailer;
        $this->unknown = $unknown;
    }

    public function run(string $user): void
    {
        $this->mailer->reminderSubmitWeek($user);
    }

    public function opaque(string $user): void
    {
        $this->unknown->reminderSubmitWeek($user);
    }
}
