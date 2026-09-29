<?php

namespace Eval\Iface;

interface Transport
{
    public function send(string $body): void;
}

final class SmtpTransport implements Transport
{
    public function send(string $body): void
    {
    }
}

final class NullTransport implements Transport
{
    public function send(string $body): void
    {
    }
}

final class Courier
{
    public function __construct(private readonly Transport $transport)
    {
    }

    public function deliver(string $body): void
    {
        $this->transport->send($body);
    }
}
