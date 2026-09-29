<?php

namespace Eval\Dynamic;

use Eval\Mail\Emails;
use Psr\Container\ContainerInterface;

final class Magic
{
    public function __call(string $name, array $args)
    {
        return null;
    }
}

final class Caller
{
    private string $prop = 'emails';

    public function __construct(
        private readonly Emails $emails,
        private readonly Magic $magic,
        private readonly ContainerInterface $container,
    ) {
    }

    public function dynamicProperty(string $user): void
    {
        $this->{$this->prop}->reminderSubmitWeek($user);
    }

    public function dynamicMethod(string $method, string $user): void
    {
        $this->emails->$method($user);
    }

    public function magicCall(string $user): void
    {
        $this->magic->anythingAtAll($user);
    }

    public function viaReflection(object $target): void
    {
        (new \ReflectionClass($target))->getMethod('reminderSubmitWeek')->invoke($target);
    }

    public function viaServiceLocator(string $user): void
    {
        $this->container->get('mailer')->reminderSubmitWeek($user);
    }
}
