<?php
declare(strict_types=1);

namespace Web\Order;

use Shared\Money\Price;
use Shared\Logging\Logger;

final class OrderService
{
    public function __construct(private Logger $logger)
    {
    }

    public function total(Price $base, Price $tax): int
    {
        $sum = $base->add($tax);
        $this->logger->info('order total computed');

        return $sum->amount();
    }

    public function priceClass(): string
    {
        return Price::class;
    }
}
