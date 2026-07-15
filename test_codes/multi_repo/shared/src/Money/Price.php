<?php
declare(strict_types=1);

namespace Shared\Money;

final class Price
{
    public function __construct(private int $cents)
    {
    }

    public function add(Price $other): Price
    {
        return new Price($this->cents + $other->cents);
    }

    public function amount(): int
    {
        return $this->cents;
    }

    public function format(): string
    {
        return number_format($this->cents / 100, 2);
    }
}
