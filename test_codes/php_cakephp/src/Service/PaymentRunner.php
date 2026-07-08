<?php
declare(strict_types=1);

namespace App\Service;

use Billing\Service\Gateway;

class PaymentRunner
{
    public function settle(array $order): string
    {
        $gateway = new Gateway();

        return $gateway->charge($order);
    }
}
