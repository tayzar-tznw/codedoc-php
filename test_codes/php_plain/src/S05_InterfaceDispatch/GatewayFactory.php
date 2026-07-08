<?php
declare(strict_types=1);

namespace App\S05_InterfaceDispatch;

class GatewayFactory
{
    public function make(string $provider): PaymentGateway
    {
        return match ($provider) {
            'stripe' => new StripeGateway(),
            default => new BraintreeGateway(),
        };
    }
}
