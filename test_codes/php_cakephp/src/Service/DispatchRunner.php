<?php
declare(strict_types=1);

namespace App\Service;

use Shipping\Service\Gateway;

class DispatchRunner
{
    public function settle(array $order): string
    {
        $gateway = new Gateway();

        return $gateway->charge($order);
    }
}
