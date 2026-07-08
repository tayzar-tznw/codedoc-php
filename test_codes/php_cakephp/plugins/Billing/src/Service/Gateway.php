<?php
declare(strict_types=1);

namespace Billing\Service;

class Gateway
{
    public function charge(array $order): string
    {
        return 'billing-charged:' . ($order['id'] ?? 0);
    }
}
