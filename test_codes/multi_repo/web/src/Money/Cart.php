<?php
declare(strict_types=1);

namespace Web\Money;

// Uses the shared Price only through inline FQCNs (new / return type /
// instanceof) — no `use` import, no ::class literal.
final class Cart
{
    public function zero(): \Shared\Money\Price
    {
        return new \Shared\Money\Price(0);
    }

    public function isPrice(mixed $value): bool
    {
        return $value instanceof \Shared\Money\Price;
    }
}
