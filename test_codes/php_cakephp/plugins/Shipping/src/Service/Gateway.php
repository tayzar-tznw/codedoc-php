<?php
declare(strict_types=1);

namespace Shipping\Service;

class Gateway
{
    public function charge(array $order): string
    {
        return 'shipping-charged:' . ($order['id'] ?? 0);
    }
}
