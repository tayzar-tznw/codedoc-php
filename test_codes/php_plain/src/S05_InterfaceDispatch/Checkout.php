<?php
declare(strict_types=1);

namespace App\S05_InterfaceDispatch;

class Checkout
{
    public function __construct(private PaymentGateway $gateway)
    {
    }

    public function pay(int $amountCents): bool
    {
        return $this->gateway->authorize($amountCents);
    }

    public function payVia(string $provider, int $amountCents): bool
    {
        $gateway = (new GatewayFactory())->make($provider);

        return $gateway->authorize($amountCents);
    }

    public static function braintreeCheckout(): bool
    {
        $checkout = new Checkout(new BraintreeGateway());

        return $checkout->pay(250);
    }
}
