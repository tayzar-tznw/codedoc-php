<?php
declare(strict_types=1);

namespace App\Controller;

use Billing\Service\Gateway as BillingGateway;
use Shipping\Service\Gateway as ShippingGateway;

class OrdersController extends AppController
{
    public function pay(int $id): void
    {
        $gateway = new BillingGateway();
        $receipt = $gateway->charge(['id' => $id]);

        $this->set(compact('receipt'));
    }

    public function ship(int $id): void
    {
        $gateway = new ShippingGateway();
        $receipt = $gateway->charge(['id' => $id]);

        $this->set(compact('receipt'));
    }
}
