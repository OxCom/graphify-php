<?php

namespace Eval\Traps;

/**
 * Two unrelated classes both declaring get() and empty(). A resolver that matches on method
 * name alone produces an edge here; the only correct answer for an untyped receiver is none.
 */
final class Cache
{
    public function get(string $key): string
    {
        return $key;
    }

    public function empty(): void
    {
    }
}

final class Basket
{
    public function get(string $sku): string
    {
        return $sku;
    }

    public function empty(): void
    {
    }
}

final class Checkout
{
    private $store;

    public function __construct(private readonly Cache $cache, $store)
    {
        $this->store = $store;
    }

    public function typedGet(string $key): void
    {
        $this->cache->get($key);
    }

    public function untypedGet(string $key): void
    {
        $this->store->get($key);
    }

    public function untypedEmpty(): void
    {
        $this->store->empty();
    }
}
