<?php
declare(strict_types=1);

namespace App\S06_Traits;

class TraitConsumer
{
    public function exercise(): array
    {
        $order = new Order();
        $customer = new Customer();
        $greeter = new Greeter();

        return [
            $order->record('created'),
            $customer->touch(),
            $greeter->hello(),
            $greeter->bonjour(),
        ];
    }
}
