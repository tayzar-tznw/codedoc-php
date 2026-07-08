<?php
declare(strict_types=1);

namespace App\S02_TypedReceivers;

class Dispatcher
{
    public function __construct(private Invoice $invoice)
    {
    }

    public function dispatchInvoice(): string
    {
        return $this->invoice->send();
    }

    public function dispatchNewsletter(Newsletter $newsletter): string
    {
        return $newsletter->send();
    }

    public function dispatchAnnotated(object $mailable): string
    {
        /** @var Invoice $mailable */
        return $mailable->send();
    }
}
